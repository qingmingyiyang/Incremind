from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from backend.api.capability_admission import ReviewedCoreCapabilityRegistry, RuntimeCapabilityAdmission
from backend.memory_app.kernel.agent_runtime_composition import build_agent_runtime_composition
from backend.memory_app.kernel.agent_coordinator import AgentCoordinatorError
from core.ai_kernel import ScopedCapabilityRegistry, SQLiteAITurnStore, SynchronousAIRuntime
from core.ai_kernel.runtime import AIKernelRuntimeError
from core.ai_kernel.agent_contracts import AgentBudget
from core.effect_log import EffectLog, EffectRunner, EffectState
from tests.rebuild.test_ai_kernel_runtime import _definition, _request
from tests.rebuild.test_ai_kernel_sqlite_store import _action
from tests.rebuild.test_ai_kernel_tool_batch import BatchPlanner, EchoProvider, acquire, sqlite_batch


def agent_batch(root, *, limit=2, planner=None, approval=False, runtime_type=SynchronousAIRuntime,
                with_effects=False):
    store = SQLiteAITurnStore(root / '.rebuild-data' / 'ai-turns.sqlite3')
    registry = ScopedCapabilityRegistry()
    composition = build_agent_runtime_composition(runtime_root=root, session_store=store,
        registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(registry)))
    profile = composition.profiles.get('main.orchestrator')
    composition.profiles.update(replace(profile, revision=profile.revision+1,
        budget_limit=AgentBudget(8, limit, 64000, 12000, 600000), max_steps=8),
        expected_revision=profile.revision)
    provider = EchoProvider('memory.recall')
    registry.register(replace(_definition('memory.recall'), requires_approval=approval), provider)
    planner = planner or BatchPlanner(('memory.recall', 'memory.recall'))
    effects = EffectRunner(EffectLog(root / '.rebuild-data' / 'effects.sqlite3'),
        owner_id='budget-fixture', lease_seconds=30) if with_effects else None
    runtime = runtime_type(planner=planner, registry=registry, events=store, payloads=store,
                           state=store, effect_runner=effects)
    composition.bind_runtime(runtime)
    request = _request()
    request['capability_policy'] = {'allowed': ['memory.recall'], 'denied': [],
                                    'require_approval': ['memory.recall'] if approval else []}
    prepared = composition.coordinator.accept_and_register_main(request)
    return runtime, store, prepared, provider, planner, composition


@pytest.mark.parametrize('limit', [0, 1])
def test_agent_frozen_tool_limit_rejects_entire_oversized_batch_before_effect(tmp_path, limit):
    runtime, store, prepared, provider, _, _ = agent_batch(tmp_path, limit=limit)
    result = runtime.run_accepted_turn(prepared.run.turn_id)
    assert prepared.run.budget_limit.tool_calls == limit
    assert result.status == 'failed'
    assert provider.calls == []
    assert not any(event['type'] == 'tool.intent.recorded' for event in store.events_after(result.turn_id))


def test_agent_budget_counts_previous_single_tool_before_batch(tmp_path):
    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control=None):
            if not any(event['type'] == 'tool.completed' for event in events):
                return {'type': 'tool', 'capability_id': 'memory.recall', 'arguments': {}}
            return {'type': 'tools', 'calls': [
                {'capability_id': 'memory.recall', 'arguments': {}} for _ in range(2)]}
    runtime, store, prepared, provider, _, _ = agent_batch(tmp_path, limit=2, planner=Planner())
    result = runtime.run_accepted_turn(prepared.run.turn_id)
    assert result.status == 'failed'
    assert len(provider.calls) == 1
    assert sum(event['type'] == 'tool.intent.recorded' for event in store.events_after(result.turn_id)) == 1


def test_agent_batch_uses_frozen_run_budget_after_profile_change(tmp_path):
    runtime, _, prepared, provider, _, composition = agent_batch(tmp_path, limit=2)
    current = composition.profiles.get('main.orchestrator')
    composition.profiles.update(replace(current, revision=current.revision+1,
        budget_limit=replace(current.budget_limit, tool_calls=1)), expected_revision=current.revision)
    assert runtime.run_accepted_turn(prepared.run.turn_id).status == 'completed'
    assert len(provider.calls) == 2


def test_agent_binding_without_budget_authority_fails_closed(tmp_path):
    runtime, store, prepared, provider, planner, _ = agent_batch(tmp_path, limit=2)
    unbound = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=store, payloads=store, state=store)
    assert unbound.run_accepted_turn(prepared.run.turn_id).status == 'failed'
    assert provider.calls == []


def test_plain_turn_keeps_existing_max_steps_tool_limit(tmp_path):
    runtime, _, request, providers, _ = sqlite_batch(tmp_path, identities=('batch.a', 'batch.b'))
    runtime._max_steps = 1
    assert runtime.submit_turn(request).status == 'failed'
    assert not any(provider.calls for provider in providers)


def test_agent_batch_approval_reserves_each_invocation_once(tmp_path):
    runtime, store, prepared, provider, _, _ = agent_batch(tmp_path, limit=2, approval=True)
    receipt = runtime.run_accepted_turn(prepared.run.turn_id)
    for index in range(2):
        assert receipt.status == 'waiting_approval'
        assert provider.calls == []
        event = tuple(store.events_after(receipt.turn_id))[-1]
        action = _action(receipt.turn_id, receipt.current_sequence, event['event_id'])
        action['action_id'] += str(index)
        action['idempotency_key'] += str(index)
        receipt = runtime.apply_action(action)
    assert receipt.status == 'completed'
    assert len(provider.calls) == 2
    assert runtime.apply_action(action).replayed
    assert len(provider.calls) == 2


def test_agent_batch_restart_keeps_same_budget_reservation(tmp_path):
    class Interrupted(SynchronousAIRuntime):
        def _prepare_tool_invocation(self, *args, **kwargs):
            raise SystemExit('frozen batch before provider')
    runtime, store, prepared, provider, planner, _ = agent_batch(tmp_path,
        limit=2, runtime_type=Interrupted, with_effects=True)
    with pytest.raises(SystemExit, match='frozen batch before provider'):
        runtime.run_accepted_turn(prepared.run.turn_id)
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=store, payloads=store, state=store, effect_runner=runtime._effect_runner)
    composition = build_agent_runtime_composition(runtime_root=tmp_path, session_store=store,
        registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(ScopedCapabilityRegistry())))
    composition.bind_runtime(restarted)
    lease = acquire(store, prepared.run.turn_id)
    result = restarted.recover_accepted_turn(prepared.run.turn_id, lease)
    assert result.status == 'completed'
    assert len(provider.calls) == 2
    assert restarted.run_accepted_turn(result.turn_id, lease).replayed
    assert len(provider.calls) == 2


def test_same_turn_concurrent_batch_reservations_do_not_exceed_frozen_budget(tmp_path):
    runtime, store, prepared, provider, _, _ = agent_batch(tmp_path, limit=2)
    turn_id = prepared.run.turn_id
    steps = [(f'step-{uuid4().hex}', f'model-request-{uuid4().hex}') for _ in range(2)]
    for step, model in steps:
        runtime._append(turn_id, 'model.requested', 'running', 'concurrent planner fixture',
                        step_id=step, model_request_id=model)
    decision = {'type': 'tools', 'calls': [
        {'capability_id': 'memory.recall', 'arguments': {}} for _ in range(2)]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runtime._freeze_tool_batch, turn_id, decision, step, model)
                   for step, model in steps]
        successes = [index for index, future in enumerate(futures) if future.exception() is None]
    assert len(successes) == 1
    step, model = steps[successes[0]]
    runtime._append(turn_id, 'model.completed', 'running', 'reserved batch fixture',
                    step_id=step, model_request_id=model)
    assert runtime.run_accepted_turn(turn_id).status == 'completed'
    assert len(provider.calls) == 2


def test_agent_remaining_tool_slot_allows_one_batch_member(tmp_path):
    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control=None):
            completed = sum(event['type'] == 'tool.completed' for event in events)
            if completed == 0:
                return {'type': 'tool', 'capability_id': 'memory.recall', 'arguments': {}}
            if completed == 1:
                return {'type': 'tools', 'calls': [{'capability_id': 'memory.recall', 'arguments': {}}]}
            return {'type': 'complete', 'summary': 'two retained outputs'}
    runtime, _, prepared, provider, _, _ = agent_batch(tmp_path, limit=2, planner=Planner())
    assert runtime.run_accepted_turn(prepared.run.turn_id).status == 'completed'
    assert len(provider.calls) == 2


def test_durable_agent_batch_without_rebound_budget_authority_cannot_dispatch(tmp_path):
    class Interrupted(SynchronousAIRuntime):
        def _prepare_tool_invocation(self, *args, **kwargs):
            raise SystemExit('approved intents before dispatch')
    runtime, store, prepared, provider, planner, _ = agent_batch(tmp_path,
        limit=2, runtime_type=Interrupted, with_effects=True)
    with pytest.raises(SystemExit, match='approved intents before dispatch'):
        runtime.run_accepted_turn(prepared.run.turn_id)
    unbound = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=store, payloads=store, state=store, effect_runner=runtime._effect_runner)
    before = tuple(store.events_after(prepared.run.turn_id))
    operations = [event['correlation']['tool_call_id'] for event in before
                  if event['type'] == 'tool.intent.recorded']
    effects_before = [runtime._effect_runner.log.get(operation) for operation in operations]
    # Recovery authority unavailability is a fenced control error. It cannot
    # manufacture a tool outcome or discard the durable, unstarted batch.
    with pytest.raises(AIKernelRuntimeError, match='frozen Agent tool budget authority is unavailable'):
        unbound.run_accepted_turn(prepared.run.turn_id)
    assert provider.calls == []
    assert tuple(store.events_after(prepared.run.turn_id)) == before
    assert [runtime._effect_runner.log.get(operation) for operation in operations] == effects_before
    composition = build_agent_runtime_composition(runtime_root=tmp_path, session_store=store,
        registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(ScopedCapabilityRegistry())))
    composition.bind_runtime(unbound)
    assert unbound.run_accepted_turn(prepared.run.turn_id).status == 'completed'
    assert len(provider.calls) == 2


@pytest.mark.parametrize('field', ['run_id', 'profile_id', 'profile_revision', 'budget_snapshot_ref'])
def test_budget_reader_rejects_drifted_binding_against_real_agent_store(tmp_path, field):
    _, _, prepared, provider, _, composition = agent_batch(tmp_path, limit=2)
    request = dict(prepared.request)
    request['agent_binding'] = dict(request['agent_binding'])
    request['agent_binding'][field] = 999 if field == 'profile_revision' else 'synthetic-drift'
    with pytest.raises((ValueError, AgentCoordinatorError)):
        composition._frozen_tool_call_limit(request)
    assert provider.calls == []


def test_recorded_single_tool_recovery_requires_frozen_budget_authority(tmp_path):
    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control=None):
            if not any(event['type'] == 'tool.completed' for event in events):
                return {'type': 'tool', 'capability_id': 'memory.recall', 'arguments': {}}
            return {'type': 'complete', 'summary': 'one retained output'}
    class Interrupted(SynchronousAIRuntime):
        def _prepare_tool_invocation(self, *args, **kwargs):
            raise SystemExit('single intent before effect claim')
    runtime, store, prepared, provider, planner, _ = agent_batch(tmp_path,
        limit=1, planner=Planner(), runtime_type=Interrupted, with_effects=True)
    turn_id = prepared.run.turn_id
    with pytest.raises(SystemExit, match='single intent before effect claim'):
        runtime.run_accepted_turn(turn_id)
    restarted_store = SQLiteAITurnStore(tmp_path / '.rebuild-data' / 'ai-turns.sqlite3')
    restarted_effects = EffectRunner(EffectLog(tmp_path / '.rebuild-data' / 'effects.sqlite3'),
        owner_id='restarted-budget-fixture', lease_seconds=30)
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=restarted_store, payloads=restarted_store, state=restarted_store,
        effect_runner=restarted_effects)
    before = tuple(restarted_store.events_after(turn_id))
    intents = [event for event in before if event['type'] == 'tool.intent.recorded']
    assert len(intents) == 1
    operation = intents[0]['correlation']['tool_call_id']
    effect_before = restarted_effects.log.get(operation)
    assert effect_before.state is EffectState.PLANNED
    assert not any(event['type'] == 'tool.started' for event in before)
    lease = acquire(restarted_store, turn_id)
    resume = restarted.recover_accepted_turn
    with pytest.raises(AIKernelRuntimeError, match='frozen Agent tool budget authority is unavailable'):
        resume(turn_id, lease)
    assert provider.calls == []
    assert tuple(restarted_store.events_after(turn_id)) == before
    assert restarted_effects.log.get(operation) == effect_before
    composition = build_agent_runtime_composition(runtime_root=tmp_path, session_store=restarted_store,
        registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(ScopedCapabilityRegistry())))
    composition.bind_runtime(restarted)
    assert prepared.run.budget_limit.tool_calls == composition._frozen_tool_call_limit(prepared.request) == 1
    result = resume(turn_id, lease)
    assert result.status == 'completed'
    assert len(provider.calls) == 1 and provider.calls[0]['tool_call_id'] == operation
    events = tuple(restarted_store.events_after(turn_id))
    assert sum(event['type'] == 'tool.intent.recorded' for event in events) == 1
    assert sum(event['type'] == 'tool.requested' for event in events) == 1
    assert sum(event['type'] == 'tool.started' for event in events) == 1
    assert restarted_effects.log.get(operation).state is EffectState.SETTLED_OK
    assert restarted.run_accepted_turn(turn_id, lease).replayed
    assert len(provider.calls) == 1


def test_plain_run_replanning_missing_budget_does_not_execute_recorded_single_intent(tmp_path):
    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control=None):
            return {'type': 'tool', 'capability_id': 'memory.recall', 'arguments': {}}
    class Interrupted(SynchronousAIRuntime):
        def _prepare_tool_invocation(self, *args, **kwargs):
            raise SystemExit('single intent before effect claim')
    runtime, store, prepared, provider, planner, _ = agent_batch(tmp_path,
        limit=1, planner=Planner(), runtime_type=Interrupted, with_effects=True)
    turn_id = prepared.run.turn_id
    with pytest.raises(SystemExit, match='single intent before effect claim'):
        runtime.run_accepted_turn(turn_id)
    restarted_store = SQLiteAITurnStore(tmp_path / '.rebuild-data' / 'ai-turns.sqlite3')
    restarted_effects = EffectRunner(EffectLog(tmp_path / '.rebuild-data' / 'effects.sqlite3'),
        owner_id='restarted-plain-run', lease_seconds=30)
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=restarted_store, payloads=restarted_store, state=restarted_store,
        effect_runner=restarted_effects)
    before = tuple(restarted_store.events_after(turn_id))
    intent_event = next(event for event in before if event['type'] == 'tool.intent.recorded')
    operation = intent_event['correlation']['tool_call_id']
    effect_before = restarted_effects.log.get(operation)
    assert effect_before.state is EffectState.PLANNED
    # Plain run retains the existing planner convergence path, rather than
    # claiming that it is the audited single-tool recovery entry point.
    assert restarted.run_accepted_turn(turn_id, acquire(restarted_store, turn_id)).status == 'failed'
    assert provider.calls == []
    events = tuple(restarted_store.events_after(turn_id))
    assert [event for event in events if event['type'] == 'tool.intent.recorded'] == [intent_event]
    assert not any(event['type'] == 'tool.started' for event in events)
    assert restarted_effects.log.get(operation) == effect_before
