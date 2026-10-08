"""Real durable action and recovery entrypoints retain their decision versions."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.memory_app.kernel.policy_runtime import ProductPolicyRuntime
from backend.memory_app.v2.policies import override, register, version
from core.ai_kernel import AIKernelContractError, RunLeaseRevoked, SynchronousAIRuntime
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from tests.rebuild.test_ai_kernel_sqlite_store import _action, _Planner, _Provider, _registry, _request


@pytest.fixture(scope='module', autouse=True)
def variants():
    register('rank', '@9841')(lambda value: 'frozen leaf')
    register('rank', '@9842')(lambda value: 'caller leaf')


class Provider(_Provider):
    def __init__(self, seen, *, receipt, fail=False):
        super().__init__(receipt=receipt)
        self.seen, self.fail = seen, fail

    def invoke(self, request):
        self.seen.append(('provider', version('rank')))
        if self.fail:
            raise RuntimeError('synthetic tool failure')
        return super().invoke(request)


def make(path, seen, *, approval=False, crash=False, selected='@9841', effects=False, fail=False):
    store = SQLiteAITurnStore(path)
    capability = 'document.draft' if approval else 'memory.recall'
    provider = Provider(seen, receipt=approval, fail=fail)
    registry = _registry(capability, 'write' if approval else 'read', approval,
        'receipt_required' if approval else 'read_only', provider)

    class Planner(_Planner):
        def plan(self, *args, **kwargs):
            seen.append(('planner', version('rank')))
            return super().plan(*args, **kwargs)

    class Crash(ProductPolicyRuntime):
        def _execute_tool_intent(self, *args, **kwargs):
            raise SystemExit('before dispatch')

    cls = Crash if crash else ProductPolicyRuntime
    runner = cls(planner=Planner(capability), registry=registry, events=store,
        payloads=store, state=store, effect_runner=store.effect_runner if effects else None)
    value = _request()
    value['capability_policy'] = {'allowed': [capability], 'denied': [],
        'require_approval': [capability] if approval else []}
    if selected is not None:
        value['policy_versions'] = {'rank': selected}
    return runner, store, provider, value


def lease(store, turn_id):
    now = datetime.now(timezone.utc)
    token = store.try_acquire_run_lease(turn_id, 'policy-recovery', now=now,
        stale_after=now + timedelta(seconds=30))
    assert token is not None
    return token


@pytest.mark.parametrize('selected,expected', [('@9841', '@9841'), (None, '@1')])
@pytest.mark.parametrize('fail', [False, True])
def test_approved_pending_action_binds_real_provider_and_replanning(tmp_path, selected, expected, fail):
    seen = []
    runner, store, provider, value = make(tmp_path / 'turn.sqlite3', seen,
        approval=True, selected=selected, fail=fail)
    waiting = runner.submit_turn(value)
    target = store.events_after(waiting.turn_id)[-1]['event_id']
    seen.clear()
    with override(rank='@9842'):
        result = runner.apply_action(_action(waiting.turn_id, waiting.current_sequence, target))
        assert version('rank') == '@9842'
    assert ('provider', expected) in seen
    assert all(selection == expected for _, selection in seen)
    assert result.status == ('failed' if fail else 'completed')
    assert provider.calls == (0 if fail else 1)


@pytest.mark.parametrize('selected,expected', [('@9841', '@9841'), (None, '@1')])
def test_recovery_of_accepted_request_binds_real_planner_and_provider(tmp_path, selected, expected):
    path, seen = tmp_path / 'turn.sqlite3', []
    first, _, _, value = make(path, seen, selected=selected)
    accepted = first.accept_turn(value)
    resumed, store, provider, _ = make(path, seen)
    with override(rank='@9842'):
        result = resumed.recover_accepted_turn(accepted.turn_id, lease(store, accepted.turn_id))
        assert version('rank') == '@9842'
    assert result.status == 'completed' and provider.calls == 1
    assert seen and all(selection == expected for _, selection in seen)


@pytest.mark.parametrize('effects', [False, True])
@pytest.mark.parametrize('selected,expected', [('@9841', '@9841'), (None, '@1')])
def test_recorded_zero_attempt_or_planned_intent_recovery_binds_actual_provider(tmp_path, effects, selected, expected):
    path, seen = tmp_path / 'turn.sqlite3', []
    first, old_store, old_provider, value = make(path, seen, crash=True,
        effects=effects, selected=selected)
    with pytest.raises(SystemExit, match='before dispatch'):
        first.submit_turn(value)
    assert old_provider.calls == 0
    events = old_store.events_after(value['turn_id'])
    assert sum(event['type'] == 'tool.intent.recorded' for event in events) == 1
    assert not any(event['type'].startswith('tool.attempt.') for event in events)
    if effects:
        from core.effect_log import EffectState
        intent = next(event for event in events if event['type'] == 'tool.intent.recorded')
        effect = old_store.effect_runner.log.get(intent['correlation']['tool_call_id'])
        assert effect.state is EffectState.PLANNED and effect.attempt == 0
    seen.clear()
    resumed, store, provider, _ = make(path, seen, effects=effects)
    with override(rank='@9842'):
        result = resumed.recover_accepted_turn(value['turn_id'], lease(store, value['turn_id']))
        assert version('rank') == '@9842'
    assert result.status == 'completed' and provider.calls == 1
    assert seen[0] == ('provider', expected)
    assert all(selection == expected for _, selection in seen)


def test_recovery_missing_version_prevents_recorded_intent_dispatch_and_restores_context(tmp_path, monkeypatch):
    from backend.memory_app.v2 import policies
    policies.register('rank', '@9833')(lambda value: 'previous process')
    path, seen = tmp_path / 'turn.sqlite3', []
    first, _, _, value = make(path, seen, crash=True, selected='@9833')
    # A prior process may have supported a version unavailable after restart.
    with pytest.raises(SystemExit, match='before dispatch'):
        first.submit_turn(value)
    seen.clear()
    monkeypatch.delitem(policies._REGISTRY['rank'], '@9833')
    resumed, store, provider, _ = make(path, seen)
    with override(rank='@9842'):
        with pytest.raises(ValueError, match='unknown_policy_version'):
            resumed.recover_accepted_turn(value['turn_id'], lease(store, value['turn_id']))
        assert version('rank') == '@9842'
    assert seen == [] and provider.calls == 0


def test_lease_and_action_validation_keep_canonical_priority_before_policy_lookup(tmp_path):
    seen = []
    runner, store, _, value = make(tmp_path / 'turn.sqlite3', seen, selected='@9299')
    accepted = runner.accept_turn(value)
    token = lease(store, accepted.turn_id)
    stale = replace(token, generation=token.generation + 1)
    with pytest.raises(RunLeaseRevoked):
        runner.run_accepted_turn(accepted.turn_id, run_lease=stale)
    with pytest.raises(RunLeaseRevoked):
        runner.recover_accepted_turn(accepted.turn_id, stale)
    with pytest.raises(RunLeaseRevoked):
        runner.apply_action({'turn_id': accepted.turn_id}, run_lease=stale)
    with pytest.raises(AIKernelContractError):
        runner.apply_action({'turn_id': accepted.turn_id}, run_lease=token)
    assert seen == []


def test_unknown_turn_keeps_base_runtime_error_and_invalid_action_contract(tmp_path):
    runner, store, _, _ = make(tmp_path / 'turn.sqlite3', [])
    base = SynchronousAIRuntime(planner=_Planner('memory.recall'),
        registry=runner._registry, events=store, payloads=store, state=store)
    for call in (lambda target: target.run_accepted_turn('turn-unknown'),
                 lambda target: target.apply_action({})):
        with pytest.raises(Exception) as original:
            call(base)
        with pytest.raises(type(original.value)) as actual:
            call(runner)
        assert str(actual.value) == str(original.value)
