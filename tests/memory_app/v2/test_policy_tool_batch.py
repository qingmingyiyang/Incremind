"""Real batch/action leaves retain the product Turn's durable policy map."""
from dataclasses import replace

import pytest

from backend.memory_app.kernel.policy_runtime import ProductPolicyRuntime
from backend.memory_app.v2 import policies
from backend.memory_app.v2.policies import ACTIVE, get, override, register, version
from backend.memory_app.v2.policies.types import RankInput
from core.ai_kernel import AIKernelContractError, RunLeaseRevoked, ScopedCapabilityRegistry
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.effect_log import EffectLog, EffectReaper, EffectRunner, EffectState
from tests.rebuild.test_ai_kernel_sqlite_store import (
    _RequestStateInitialProvider, _RequestStateContinuationProvider, _action, _mcp_action,
    _native_mcp_definition,
)
from tests.rebuild.test_ai_kernel_tool_batch import acquire, mcp_batch, sqlite_batch


@pytest.fixture(scope='module', autouse=True)
def variants():
    register('rank', '@9941')(lambda value: 41)
    register('rank', '@9942')(lambda value: 42)
    register('rank', '@9943')(lambda value: 43)


@pytest.fixture(autouse=True)
def restore_active(monkeypatch):
    # Providers deliberately change ACTIVE during dispatch. Preserve the
    # original global value even when the first observation precedes an action.
    monkeypatch.setitem(ACTIVE, 'rank', ACTIVE['rank'])


def observe(seen, identity):
    before = (version('rank'), get('rank')(RankInput(1, 2)))
    ACTIVE['rank'] = '@9943'
    after = (version('rank'), get('rank')(RankInput(1, 2)))
    seen.append((identity, before, after))


def freeze(request, selected):
    if selected is not None:
        request['policy_versions'] = {'rank': selected}
    return request


def reopen(tmp_path, runtime, planner):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    effects = EffectRunner(EffectLog(tmp_path / 'effects.sqlite3'),
        owner_id='policy-batch-restart', lease_seconds=30)
    restored = ProductPolicyRuntime(planner=planner, registry=runtime._registry,
        events=store, payloads=store, state=store, effect_runner=effects,
        mcp_continuation_reconnector=runtime._mcp_continuation_reconnector)
    return restored, store


def policy_mcp(tmp_path, seen, selected, *, outcome='success'):
    runtime, store, request, providers, planner, _ = mcp_batch(tmp_path,
        runtime_type=ProductPolicyRuntime,
        sibling_callback=lambda request: observe(seen, 'sibling'))
    class Initial(_RequestStateInitialProvider):
        calls = 0
        def invoke(self, request):
            self.calls += 1
            observe(seen, 'initial')
            return super().invoke(request)
    class Continuation(_RequestStateContinuationProvider):
        def continue_request_state(self, request, state):
            observe(seen, 'continuation')
            return super().continue_request_state(request, state)
    initial, fresh = Initial(), Continuation(outcome)
    definition = _native_mcp_definition('calendar-server')
    sibling_definition = runtime._registry.resolve('batch.b')[0]
    runtime._registry = ScopedCapabilityRegistry()
    registrations = [runtime._registry.register(definition, initial)]
    runtime._registry.register(sibling_definition, providers[1])
    def reconnect(server):
        assert server == 'calendar-server'
        registrations[-1].close()
        registrations.append(runtime._registry.register(definition, fresh))
    runtime._mcp_continuation_reconnector = reconnect
    return runtime, store, freeze(request, selected), providers, planner, initial, fresh


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('selected,expected', [('@9941', ('@9941', 41)), (None, ('@1', 4))])
def test_approved_batch_action_executes_frozen_policy_before_replanning(tmp_path, monkeypatch, restart, selected, expected):
    seen = []
    runtime, store, request, providers, planner = sqlite_batch(tmp_path,
        identities=('batch.a', 'batch.b'), approval=('batch.b',), runtime_type=ProductPolicyRuntime,
        callbacks={name: lambda request: observe(seen, 'provider') for name in ('batch.a', 'batch.b')})
    waiting = runtime.submit_turn(freeze(request, selected))
    assert waiting.status == 'waiting_approval'
    assert not any(provider.calls for provider in providers)
    if restart:
        runtime, store = reopen(tmp_path, runtime, planner)
    original = store.get_request(waiting.turn_id)
    event = tuple(store.events_after(waiting.turn_id))[-1]
    action = _action(waiting.turn_id, waiting.current_sequence, event['event_id'])
    monkeypatch.setitem(ACTIVE, 'rank', '@9942')
    with override(rank='@9942'):
        result = runtime.apply_action(action, acquire(store, waiting.turn_id) if restart else None)
        assert version('rank') == '@9942' and get('rank')(RankInput(1, 2)) == 42
    assert result.status == 'completed'
    assert len(seen) == 2 and all(before == after == expected for _, before, after in seen)
    assert [len(provider.calls) for provider in providers] == [1, 1]
    assert store.get_request(waiting.turn_id) == original
    completed = [event['data']['capability_id'] for event in store.events_after(waiting.turn_id)
                 if event['type'] == 'tool.completed']
    assert completed == ['batch.a', 'batch.b']


@pytest.mark.parametrize('entry', ['run', 'recover'])
def test_restart_of_recorded_batch_uses_frozen_policy_for_real_providers(tmp_path, monkeypatch, entry):
    seen = []
    class Interrupted(ProductPolicyRuntime):
        def _prepare_tool_invocation(self, *args, **kwargs):
            raise SystemExit('batch intents before provider')
    runtime, _, request, providers, planner = sqlite_batch(tmp_path,
        identities=('batch.a', 'batch.b'), runtime_type=Interrupted,
        callbacks={name: lambda request: observe(seen, 'provider') for name in ('batch.a', 'batch.b')})
    with pytest.raises(SystemExit, match='batch intents before provider'):
        runtime.submit_turn(freeze(request, '@9941'))
    assert seen == []
    runtime, store = reopen(tmp_path, runtime, planner)
    before = tuple(store.events_after(request['turn_id']))
    intent_ids = [event['correlation']['tool_call_id'] for event in before if event['type'] == 'tool.intent.recorded']
    assert len(intent_ids) == 2
    assert all(runtime._effect_runner.log.get(identity).state is EffectState.PLANNED for identity in intent_ids)
    monkeypatch.setitem(ACTIVE, 'rank', '@9942')
    with override(rank='@9942'):
        resume = runtime.run_accepted_turn if entry == 'run' else runtime.recover_accepted_turn
        result = resume(request['turn_id'], acquire(store, request['turn_id']))
        assert version('rank') == '@9942'
    assert result.status == 'completed'
    assert len(seen) == 2 and all(before == after == ('@9941', 41) for _, before, after in seen)
    assert [len(provider.calls) for provider in providers] == [1, 1]
    assert all(runtime._effect_runner.log.get(identity).state is EffectState.SETTLED_OK for identity in intent_ids)


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('selected,expected', [('@9941', ('@9941', 41)), (None, ('@1', 4))])
def test_manual_mcp_continuation_binds_frozen_policy_on_new_effect(tmp_path, monkeypatch, restart, selected, expected):
    seen = []
    runtime, store, request, providers, planner, initial, fresh = policy_mcp(tmp_path, seen, selected)
    waiting = runtime.submit_turn(request)
    assert waiting.status == 'waiting_approval'
    assert initial.calls == 1 and fresh.continuation_calls == 0
    if restart:
        runtime, store = reopen(tmp_path, runtime, planner)
    before = tuple(store.events_after(waiting.turn_id))
    call_id = before[-1]['correlation']['tool_call_id']
    action = _mcp_action(waiting, runtime)
    monkeypatch.setitem(ACTIVE, 'rank', '@9942')
    with override(rank='@9942'):
        result = runtime.apply_action(action, acquire(store, waiting.turn_id) if restart else None)
        assert version('rank') == '@9942' and get('rank')(RankInput(1, 2)) == 42
    assert result.status == 'completed'
    assert len(seen) == 3 and all(before == after == expected for _, before, after in seen)
    assert initial.calls == 1 and fresh.continuation_calls == 1 and len(providers[1].calls) == 1
    child = runtime._effect_runner.log.get(f'{call_id}.continue.2')
    assert child.parent_id == call_id and child.state is EffectState.SETTLED_OK
    assert store.get_request(waiting.turn_id) == request


def test_failed_mcp_continuation_restores_caller_policy_and_keeps_unknown_effect(tmp_path, monkeypatch):
    seen = []
    runtime, store, request, providers, planner, initial, fresh = policy_mcp(tmp_path, seen, '@9941', outcome='unknown')
    waiting = runtime.submit_turn(request)
    runtime, store = reopen(tmp_path, runtime, planner)
    action = _mcp_action(waiting, runtime)
    call_id = tuple(store.events_after(waiting.turn_id))[-1]['correlation']['tool_call_id']
    token = acquire(store, waiting.turn_id)
    monkeypatch.setitem(ACTIVE, 'rank', '@9942')
    with override(rank='@9942'):
        result = runtime.apply_action(action, token)
        assert version('rank') == '@9942'
    assert result.status == 'failed'
    assert len(seen) == 3 and all(before == after == ('@9941', 41) for _, before, after in seen)
    child = runtime._effect_runner.log.get(f'{call_id}.continue.2')
    assert child.state is EffectState.INFLIGHT
    outcomes = EffectReaper(runtime._effect_runner.log).recover_expired(now=child.lease_expires_at + 1)
    assert any(item.operation_id == child.operation_id and item.state is EffectState.UNKNOWN for item in outcomes)
    assert runtime._effect_runner.log.get(child.operation_id).state is EffectState.UNKNOWN
    assert runtime.run_accepted_turn(waiting.turn_id, token).replayed
    assert initial.calls == 1 and fresh.continuation_calls == 1 and len(providers[1].calls) == 1


@pytest.mark.parametrize('leaf', ['approval', 'mcp_continue'])
def test_missing_frozen_policy_prevents_new_wire_after_canonical_action_guards(tmp_path, monkeypatch, leaf):
    seen = []
    if leaf == 'approval':
        runtime, store, request, providers, planner = sqlite_batch(tmp_path,
            identities=('batch.a', 'batch.b'), approval=('batch.b',), runtime_type=ProductPolicyRuntime,
            callbacks={name: lambda request: observe(seen, 'provider') for name in ('batch.a', 'batch.b')})
        waiting = runtime.submit_turn(freeze(request, '@9941'))
        event = tuple(store.events_after(waiting.turn_id))[-1]
        action = _action(waiting.turn_id, waiting.current_sequence, event['event_id'])
    else:
        runtime, store, request, providers, planner, initial, fresh = policy_mcp(tmp_path, seen, '@9941')
        waiting = runtime.submit_turn(request)
        action = _mcp_action(waiting, runtime)
    runtime, store = reopen(tmp_path, runtime, planner)
    seen.clear()
    monkeypatch.setitem(ACTIVE, 'rank', '@9942')
    monkeypatch.delitem(policies._REGISTRY['rank'], '@9941')
    token = acquire(store, waiting.turn_id)
    before = tuple(store.events_after(waiting.turn_id))
    with override(rank='@9942'):
        with pytest.raises(RunLeaseRevoked):
            runtime.apply_action(action, replace(token, generation=token.generation + 1))
        with pytest.raises(AIKernelContractError):
            runtime.apply_action({'turn_id': waiting.turn_id}, token)
        assert tuple(store.events_after(waiting.turn_id)) == before
        if leaf == 'approval':
            assert runtime.apply_action(action, token).status == 'failed'
            assert not any(provider.calls for provider in providers)
        else:
            with pytest.raises(ValueError, match='unknown_policy_version'):
                runtime.apply_action(action, token)
            assert initial.calls == 1 and fresh.continuation_calls == 0 and len(providers[1].calls) == 1
            call_id = before[-1]['correlation']['tool_call_id']
            with pytest.raises(KeyError):
                runtime._effect_runner.log.get(f'{call_id}.continue.2')
        assert version('rank') == '@9942' and get('rank')(RankInput(1, 2)) == 42
    assert seen == []
    assert sum(event['type'] == 'tool.started' for event in store.events_after(waiting.turn_id)) == sum(
        event['type'] == 'tool.started' for event in before)
