from contextvars import ContextVar
from dataclasses import replace
from threading import Barrier, Event
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from time import monotonic

import pytest

from core.ai_kernel import (
    CapabilityDefinition, InMemoryTurnEventStore, InMemoryTurnPayloadStore,
    ScopedCapabilityRegistry, SynchronousAIRuntime, ToolExecutionBoundaryDecision,
)
from core.ai_tooling.contracts import tool_from_legacy_capability
from core.ai_kernel.model_planner import _validate_decision
from tests.rebuild.test_ai_kernel_runtime import _definition, _request
from tests.rebuild.test_ai_kernel_sqlite_store import _action
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.effect_log import EffectLog, EffectRunner
from core.ai_kernel.dispatcher import ToolProviderFailure
from tests.rebuild.test_ai_kernel_sqlite_store import _RequestStateInitialProvider, _RequestStateContinuationProvider, _native_mcp_definition, _mcp_action


def test_model_planner_keeps_an_ordered_tool_batch():
    value = {'type': 'tools', 'calls': [
        {'capability_id': identity, 'arguments': {'n': index}}
        for index, identity in enumerate(('batch.first', 'batch.second'))
    ]}
    assert _validate_decision(value, {'batch.first', 'batch.second'}) == value


def test_real_runtime_preflights_then_overlaps_reads_and_draft_and_saves_in_order():
    identities = ('batch.first', 'batch.second', 'batch.draft')
    preflight, finished = [], []
    entered = Barrier(3)
    second_done, third_done = Event(), Event()
    ambient = ContextVar('synthetic-tool-policy', default='absent')

    class Boundary:
        def evaluate(self, request, definition, decision):
            preflight.append(definition.capability_id)
            return ToolExecutionBoundaryDecision('allow', (), (), 1, False, False, {})

    class Provider:
        def __init__(self, index):
            self.index = index

        def invoke(self, request):
            assert preflight == list(identities)
            assert ambient.get() == 'frozen@1'
            entered.wait(timeout=2)
            if self.index == 0:
                assert second_done.wait(2)
            elif self.index == 1:
                assert third_done.wait(2)
            finished.append(identities[self.index])
            if self.index == 2:
                third_done.set()
            elif self.index == 1:
                second_done.set()
            return {'summary': identities[self.index], 'result': {'index': self.index},
                    'receipt_ref': f'crp://synthetic/operations/{self.index}'}

    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control=None):
            completed = [event for event in events if event['type'] == 'tool.completed']
            if completed:
                assert [event['data']['capability_id'] for event in completed] == list(identities)
                return {'type': 'complete', 'summary': 'all outputs retained'}
            return {'type': 'tools', 'calls': [
                {'capability_id': identity, 'arguments': {}} for identity in identities
            ]}

    registry = ScopedCapabilityRegistry()
    for index, identity in enumerate(identities):
        definition = _definition(identity)
        if index == 2:
            definition = CapabilityDefinition(identity, 1, 'write', False, 'receipt_required',
                definition.input_schema_uri, definition.output_schema_uri)
            definition = replace(definition, tool_definition=tool_from_legacy_capability(
                definition, boundary_requirements=('draft_create_only',)))
        registry.register(definition, Provider(index))
    events, payloads = InMemoryTurnEventStore(), InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(planner=Planner(), registry=registry, events=events,
                                  payloads=payloads, execution_boundary=Boundary())
    request = _request()
    request['capability_policy'] = {'allowed': list(identities), 'denied': [], 'require_approval': []}
    token = ambient.set('frozen@1')
    try:
        receipt = runtime.submit_turn(request)
    finally:
        ambient.reset(token)
    assert receipt.status == 'completed'
    assert finished == list(reversed(identities))
    recorded = tuple(runtime.events_after(receipt.turn_id))
    for kind in ('tool.intent.recorded', 'tool.outcome.recorded', 'tool.completed'):
        assert [event['data']['capability_id'] for event in recorded if event['type'] == kind] == list(identities)
    assert len({event['correlation']['tool_call_id'] for event in recorded
                if event['type'] == 'tool.intent.recorded'}) == 3


class BatchPlanner:
    def __init__(self, identities):
        self.identities, self.calls = identities, 0
        self.arguments = {}

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        self.calls += 1
        terminal = [event for event in events if event['type'] == 'tool.completed']
        if terminal:
            assert [event['data']['capability_id'] for event in terminal] == list(self.identities)
            return {'type': 'complete', 'summary': 'batch outputs ready'}
        return {'type': 'tools', 'calls': [
            {'capability_id': identity, 'arguments': self.arguments.get(identity, {})} for identity in self.identities
        ]}


class EchoProvider:
    def __init__(self, identity, callback=None):
        self.identity, self.callback, self.calls = identity, callback, []

    def invoke(self, request):
        self.calls.append(request)
        if self.callback:
            self.callback(request)
        return {'summary': self.identity, 'result': {'identity': self.identity},
                'operation_receipt': {'operation': self.identity, 'status': 'completed'}}


def sqlite_batch(tmp_path, *, identities=('batch.a', 'batch.b', 'batch.c'),
                 approval=(), modes=None, callbacks=None, boundary=None, runtime_type=SynchronousAIRuntime,
                 with_effects=True):
    registry = ScopedCapabilityRegistry()
    providers = []
    for identity in identities:
        definition = _definition(identity)
        mode = (modes or {}).get(identity, 'read')
        if mode != 'read':
            definition = replace(definition, mode='write', operation_semantics='receipt_required',
                                 requires_approval=identity in approval)
            tool = tool_from_legacy_capability(definition, boundary_requirements=(
                ('draft_create_only',) if mode == 'draft' else ()))
            definition = replace(definition, tool_definition=tool)
        elif identity in approval:
            definition = replace(definition, requires_approval=True)
        provider = EchoProvider(identity, (callbacks or {}).get(identity))
        registry.register(definition, provider)
        providers.append(provider)
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    planner = BatchPlanner(identities)
    effects = EffectRunner(EffectLog(tmp_path / 'effects.sqlite3'), owner_id='batch-runner', lease_seconds=30)
    runtime = runtime_type(planner=planner, registry=registry, events=store, payloads=store, state=store,
                           effect_runner=effects if with_effects else None, execution_boundary=boundary)
    request = _request()
    request['capability_policy'] = {'allowed': list(identities), 'denied': [], 'require_approval': list(approval)}
    return runtime, store, request, providers, planner


def acquire(store, turn_id):
    now = datetime.now(timezone.utc)
    token = store.try_acquire_run_lease(turn_id, 'batch-test', now=now, stale_after=now+timedelta(seconds=30))
    assert token is not None
    return token


def test_sqlite_batch_waits_for_all_approvals_before_any_effect_and_replays_action(tmp_path):
    runtime, store, request, providers, planner = sqlite_batch(tmp_path, approval=('batch.b', 'batch.c'))
    waiting = runtime.submit_turn(request)
    assert waiting.status == 'waiting_approval'
    assert [len(provider.calls) for provider in providers] == [0, 0, 0]
    for expected in ('batch.b', 'batch.c'):
        event = tuple(store.events_after(waiting.turn_id))[-1]
        assert event['data']['capability_id'] == expected
        action = _action(waiting.turn_id, waiting.current_sequence, event['event_id'])
        action['idempotency_key'] += expected
        action['action_id'] += expected
        waiting = runtime.apply_action(action)
        if expected == 'batch.b':
            assert waiting.status == 'waiting_approval'
            assert not any(provider.calls for provider in providers)
    assert waiting.status == 'completed'
    assert [len(provider.calls) for provider in providers] == [1, 1, 1]
    assert planner.calls == 2
    assert runtime.apply_action(action).replayed


def test_sqlite_batch_restart_after_all_intents_uses_frozen_members_before_planner(tmp_path):
    class Interrupted(SynchronousAIRuntime):
        def _prepare_tool_invocation(self, *args, **kwargs):
            raise SystemExit('before batch dispatch')
    runtime, store, request, providers, planner = sqlite_batch(tmp_path, runtime_type=Interrupted)
    with pytest.raises(SystemExit, match='before batch dispatch'):
        runtime.submit_turn(request)
    assert not any(provider.calls for provider in providers)
    assert sum(event['type'] == 'tool.intent.recorded' for event in store.events_after(request['turn_id'])) == 3
    restarted_store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry, events=restarted_store,
        payloads=restarted_store, state=restarted_store, effect_runner=runtime._effect_runner)
    completed = restarted.recover_accepted_turn(request['turn_id'], acquire(restarted_store, request['turn_id']))
    assert completed.status == 'completed'
    assert [len(provider.calls) for provider in providers] == [1, 1, 1]
    assert planner.calls == 2
    assert sum(event['type'] == 'tool.intent.recorded' for event in restarted_store.events_after(request['turn_id'])) == 3


def test_sqlite_batch_background_cancel_reaches_every_active_provider(tmp_path):
    entered = Barrier(4)
    cancelled = []
    def callback(request):
        entered.wait(timeout=5)
        deadline = monotonic()+5
        while not request['execution_context'].cancel_requested and monotonic() < deadline:
            Event().wait(.005)
        assert request['execution_context'].cancel_requested
        cancelled.append(request['capability_id'])
        request['execution_context'].checkpoint()
    identities = ('batch.a', 'batch.b', 'batch.c')
    runtime, store, request, providers, _ = sqlite_batch(tmp_path, callbacks=dict.fromkeys(identities, callback))
    receipt = runtime.accept_turn(request)
    lease = acquire(store, receipt.turn_id)
    with ThreadPoolExecutor(max_workers=1) as executor:
        run = executor.submit(runtime.run_accepted_turn, receipt.turn_id, lease)
        entered.wait(timeout=5)
        assert runtime.request_background_cancel(receipt.turn_id, lease)
        final = run.result(timeout=8)
    assert final.status == 'cancelled'
    assert set(cancelled) == set(identities)
    events = tuple(store.events_after(receipt.turn_id))
    assert sum(event['type'] == 'tool.cancelled' for event in events) == 3
    assert [event['sequence'] for event in events] == list(range(1, len(events)+1))
    assert events[-1]['type'] == 'turn.cancelled'


def test_sqlite_batch_workers_share_frozen_context_and_record_wire_receipts(tmp_path):
    entered, wire_entered = Barrier(3), Barrier(3)
    ambient = ContextVar('batch-policy', default='absent')
    observed = []
    def callback(request):
        entered.wait(timeout=5)
        assert ambient.get() == 'frozen@1'
        context = request['execution_context']
        handle = context.take_nested_model_handle(invocation_key='primary', purpose='primary')
        handle.model_call_routed(snapshot_ref=f"crp://session/{request['turn_id']}/model-route/frozen",
            snapshot_revision='a'*64, prompt_cache_scope_identity='b'*64,
            provider='synthetic', model='synthetic', execution_location='remote')
        attempt = handle.begin_model_wire_attempt()
        handle.model_call_started(provider='synthetic', model='synthetic')
        def wire():
            wire_entered.wait(timeout=5)
            observed.append(request['capability_id'])
            attempt.succeeded(usage={'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3}, cache_observation=None)
        attempt.invoke_wire(wire)
        handle.model_call_completed(usage={'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3})
        handle.finalize(error_code=None)
    identities = ('batch.a', 'batch.b', 'batch.c')
    runtime, store, request, providers, _ = sqlite_batch(tmp_path, callbacks=dict.fromkeys(identities, callback),
                                                       modes={'batch.c': 'draft'})
    receipt = runtime.accept_turn(request)
    lease = acquire(store, receipt.turn_id)
    token = ambient.set('frozen@1')
    try:
        final = runtime.run_accepted_turn(receipt.turn_id, lease)
    finally:
        ambient.reset(token)
    assert final.status == 'completed'
    assert set(observed) == set(identities)
    events = tuple(store.events_after(receipt.turn_id))
    assert [event['sequence'] for event in events] == list(range(1, len(events)+1))
    assert len([event for event in events if event['type'] == 'model.attempt.dispatched']) == 3
    receipts = [store.get(event['data']['receipt_ref']) for event in events if event['type'] == 'model.attempt.terminal']
    assert len(receipts) == 3 and all(value['status'] == 'succeeded' for value in receipts)
    assert [event['data']['capability_id'] for event in events if event['type'] == 'tool.completed'] == list(identities)


@pytest.mark.parametrize('mode', ['read', 'draft'])
def test_batch_failure_keeps_every_started_sibling_result_before_turn_terminal(tmp_path, mode):
    entered = Barrier(3)
    def callback(request):
        entered.wait(timeout=5)
        if request['capability_id'] == 'batch.a':
            raise ValueError('synthetic failure')
    runtime, store, request, providers, _ = sqlite_batch(tmp_path,
        callbacks=dict.fromkeys(('batch.a', 'batch.b', 'batch.c'), callback), modes={'batch.a': mode})
    result = runtime.submit_turn(request)
    assert result.status == 'failed'
    assert all(len(provider.calls) == 1 for provider in providers)
    events = tuple(store.events_after(result.turn_id))
    assert events[-1]['type'] == 'turn.failed'
    assert [event['data']['capability_id'] for event in events if event['type'] == 'tool.outcome.recorded'] == ['batch.a', 'batch.b', 'batch.c']
    assert [event['data']['capability_id'] for event in events if event['type'] == 'tool.completed'] == ['batch.b', 'batch.c']
    first = next(event for event in events if event['type'] == 'tool.outcome.recorded')
    assert store.get(first['data']['payload_ref'])['effect_certainty'] == ('confirmed_none' if mode == 'read' else 'unknown')
    assert runtime.submit_turn(request).replayed
    assert all(len(provider.calls) == 1 for provider in providers)


def test_batch_boundary_deny_has_no_provider_effect(tmp_path):
    visited = []
    class Boundary:
        def evaluate(self, request, definition, decision):
            visited.append(definition.capability_id)
            return ToolExecutionBoundaryDecision('deny' if definition.capability_id == 'batch.b' else 'allow',
                                                 (), (), 1, False, False, {})
    runtime, _, request, providers, _ = sqlite_batch(tmp_path, boundary=Boundary())
    assert runtime.submit_turn(request).status == 'failed'
    assert visited == ['batch.a', 'batch.b']
    assert not any(provider.calls for provider in providers)


def test_batch_mcp_wait_is_last_after_sibling_results_and_startup_is_waiting(tmp_path):
    runtime, store, request, providers, planner = sqlite_batch(tmp_path, identities=('calendar.read', 'batch.b'),
                                                              with_effects=False)
    # Replace only the external provider fixture; the real registry and runtime govern it.
    runtime._registry = ScopedCapabilityRegistry()
    registration = runtime._registry.register(_native_mcp_definition('calendar-server'), _RequestStateInitialProvider())
    runtime._registry.register(_definition('batch.b'), providers[1])
    planner.arguments = {'calendar.read': {'query': 'question'}}
    waiting = runtime.submit_turn(request)
    assert waiting.status == 'waiting_approval'
    events = tuple(store.events_after(waiting.turn_id))
    assert events[-1]['type'] == 'mcp.continuation.required'
    assert len(providers[1].calls) == 1
    from core.ai_kernel.recovery import classify_recovery
    assert classify_recovery(waiting.turn_id, 1, events, payload_loader=store.get).disposition == 'waiting_noop'
    continuation = _RequestStateContinuationProvider('success')
    def reconnect(server):
        assert server == 'calendar-server'
        registration.close()
        runtime._registry.register(_native_mcp_definition('calendar-server'), continuation)
    runtime._mcp_continuation_reconnector = reconnect
    final = runtime.apply_action(_mcp_action(waiting, runtime))
    assert final.status == 'completed'
    assert len(providers[1].calls) == 1
    assert planner.calls == 2


def test_mcp_effect_runner_requires_explicit_continue_and_uses_a_child_effect(tmp_path):
    from core.effect_log import EffectState
    runtime, store, request, providers, planner = sqlite_batch(tmp_path, identities=('calendar.read', 'batch.b'))
    runtime._registry = ScopedCapabilityRegistry()
    initial = _RequestStateInitialProvider()
    registration = runtime._registry.register(_native_mcp_definition('calendar-server'), initial)
    runtime._registry.register(_definition('batch.b'), providers[1])
    planner.arguments = {'calendar.read': {'query': 'question'}}
    waiting = runtime.submit_turn(request)
    assert waiting.status == 'waiting_approval'
    events = tuple(store.events_after(waiting.turn_id))
    call_id = events[-1]['correlation']['tool_call_id']
    parent = runtime._effect_runner.log.get(call_id)
    assert parent.state is EffectState.SETTLED_OK
    assert parent.result_ref == events[-1]['data']['payload_ref']
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry, events=store, payloads=store,
                                     state=store, effect_runner=runtime._effect_runner)
    lease = acquire(store, waiting.turn_id)
    assert restarted.recover_accepted_turn(waiting.turn_id, lease).status == 'waiting_approval'
    continuation = _RequestStateContinuationProvider('success')
    def reconnect(server):
        assert server == 'calendar-server'
        registration.close()
        runtime._registry.register(_native_mcp_definition('calendar-server'), continuation)
    restarted._mcp_continuation_reconnector = reconnect
    action = _mcp_action(waiting, restarted)
    completed = restarted.apply_action(action, lease)
    assert completed.status == 'completed'
    child = runtime._effect_runner.log.get(f'{call_id}.continue.2')
    assert child.parent_id == call_id and child.state is EffectState.SETTLED_OK
    assert child.effect_class.value == 'AT_MOST_ONCE'
    assert continuation.continuation_calls == 1
    assert restarted.apply_action(action, lease).replayed
    assert continuation.continuation_calls == 1 and len(providers[1].calls) == 1


def mcp_batch(tmp_path, *, runtime_type=SynchronousAIRuntime, continuation='success',
              sibling_mode='read', sibling_callback=None):
    runtime, store, request, providers, planner = sqlite_batch(tmp_path,
        identities=('calendar.read', 'batch.b'), runtime_type=runtime_type,
        modes={'batch.b': sibling_mode}, callbacks={'batch.b': sibling_callback})
    sibling_definition = runtime._registry.resolve('batch.b')[0]
    runtime._registry = ScopedCapabilityRegistry()
    initial = _RequestStateInitialProvider()
    leases = [runtime._registry.register(_native_mcp_definition('calendar-server'), initial)]
    runtime._registry.register(sibling_definition, providers[1])
    planner.arguments = {'calendar.read': {'query': 'question'}}
    fresh = _RequestStateContinuationProvider(continuation)
    def reconnect(server):
        assert server == 'calendar-server'
        leases[-1].close()
        leases.append(runtime._registry.register(_native_mcp_definition('calendar-server'), fresh))
    runtime._mcp_continuation_reconnector = reconnect
    return runtime, store, request, providers, planner, fresh


@pytest.mark.parametrize('action_type', ['mcp_reject', 'cancel'])
def test_mcp_batch_rejection_and_cancel_keep_already_returned_sibling(tmp_path, action_type):
    runtime, store, request, providers, planner, fresh = mcp_batch(tmp_path)
    waiting = runtime.submit_turn(request)
    action = _mcp_action(waiting, runtime)
    action['type'] = action_type
    if action_type == 'cancel':
        action['target_event_id'] = None
    result = runtime.apply_action(action)
    assert result.status == 'cancelled'
    events = tuple(store.events_after(result.turn_id))
    assert events[-1]['type'] == 'turn.cancelled'
    assert [event['data']['capability_id'] for event in events if event['type'] == 'tool.outcome.recorded'] == ['calendar.read', 'batch.b']
    assert [event['data']['capability_id'] for event in events if event['type'] == 'tool.completed'] == ['batch.b']
    assert fresh.continuation_calls == 0 and len(providers[1].calls) == 1


@pytest.mark.parametrize('stage', ['buffer_before_effect', 'buffer_after_effect',
                                  'queue_before_event', 'waiting_before_effect', 'waiting_after_effect'])
def test_mcp_batch_receipt_crash_recovery_has_no_second_provider_call(tmp_path, stage):
    class Interrupted(SynchronousAIRuntime):
        armed = True
        def _settle_buffered_effect(self, *args, **kwargs):
            if self.armed and stage == 'buffer_before_effect':
                self.armed = False
                raise SystemExit('buffer receipt durable before effect')
            result = super()._settle_buffered_effect(*args, **kwargs)
            if self.armed and stage == 'buffer_after_effect':
                self.armed = False
                raise SystemExit('buffer receipt durable before effect')
            return result
        def _persist_mcp_waiting(self, *args, **kwargs):
            if self.armed and stage == 'queue_before_event':
                self.armed = False
                raise SystemExit('queued receipt durable before effect')
            return super()._persist_mcp_waiting(*args, **kwargs)
        def _settle_mcp_initial_effect(self, *args, **kwargs):
            if self.armed and stage == 'waiting_before_effect':
                self.armed = False
                raise SystemExit('waiting receipt durable before effect')
            result = super()._settle_mcp_initial_effect(*args, **kwargs)
            if self.armed and stage == 'waiting_after_effect':
                self.armed = False
                raise SystemExit('waiting receipt durable before effect')
            return result
    runtime, store, request, providers, planner, fresh = mcp_batch(tmp_path, runtime_type=Interrupted)
    with pytest.raises(SystemExit, match='receipt durable before effect'):
        runtime.submit_turn(request)
    events = tuple(store.events_after(request['turn_id']))
    from core.ai_kernel.recovery import classify_recovery
    decision = classify_recovery(request['turn_id'], 1, events, payload_loader=store.get)
    assert decision.disposition in {'safe_resume', 'waiting_noop'}
    from core.effect_log import EffectReaper
    EffectReaper(runtime._effect_runner.log).recover_expired(now=2_000_000_000)
    restarted_store = SQLiteAITurnStore(tmp_path/'turns.sqlite3')
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=restarted_store, payloads=restarted_store, state=restarted_store,
        effect_runner=runtime._effect_runner, mcp_continuation_reconnector=runtime._mcp_continuation_reconnector)
    lease = acquire(restarted_store, request['turn_id'])
    waiting = restarted.recover_accepted_turn(request['turn_id'], lease)
    assert waiting.status == 'waiting_approval'
    completed = restarted.apply_action(_mcp_action(waiting, restarted), lease)
    assert completed.status == 'completed'
    assert fresh.continuation_calls == 1 and len(providers[1].calls) == 1
    events = tuple(restarted_store.events_after(completed.turn_id))
    assert [event['data']['capability_id'] for event in events if event['type'] == 'tool.outcome.recorded'] == ['calendar.read', 'batch.b']


@pytest.mark.parametrize('outcome', ['unknown', 'input_required'])
def test_mcp_child_unknown_and_second_wait_never_replays_and_drains_buffer(tmp_path, outcome):
    runtime, store, request, providers, planner, fresh = mcp_batch(tmp_path, continuation=outcome)
    waiting = runtime.submit_turn(request)
    action = _mcp_action(waiting, runtime)
    failed = runtime.apply_action(action)
    assert failed.status == 'failed'
    assert fresh.continuation_calls == 1 and len(providers[1].calls) == 1
    events = tuple(store.events_after(failed.turn_id))
    assert events[-1]['type'] == 'turn.failed'
    outcomes = [store.get(event['data']['payload_ref']) for event in events if event['type'] == 'tool.outcome.recorded']
    assert outcomes[0]['effect_certainty'] == 'unknown'
    assert outcomes[1]['status'] == 'completed'
    assert runtime.apply_action(action).replayed
    assert runtime.submit_turn(request).replayed
    assert fresh.continuation_calls == 1


@pytest.mark.parametrize('certainty', ['confirmed_none', 'unknown'])
def test_batch_buffered_failure_remains_failure_after_wait_restart(tmp_path, certainty):
    def fail(request):
        raise ToolProviderFailure('synthetic.failure', effect_certainty=certainty)
    runtime, store, request, providers, planner, fresh = mcp_batch(tmp_path,
        sibling_mode='draft', sibling_callback=fail)
    failed_provider = providers[1]
    waiting = runtime.submit_turn(request)
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry, events=store,
        payloads=store, state=store, effect_runner=runtime._effect_runner,
        mcp_continuation_reconnector=runtime._mcp_continuation_reconnector)
    result = restarted.apply_action(_mcp_action(waiting, restarted))
    assert result.status == 'failed'
    events = tuple(store.events_after(result.turn_id))
    outcomes = [store.get(event['data']['payload_ref']) for event in events if event['type'] == 'tool.outcome.recorded']
    assert outcomes[0]['status'] == 'completed'
    assert outcomes[1]['effect_certainty'] == certainty
    assert outcomes[1]['status'] == ('failed' if certainty == 'confirmed_none' else 'unknown_effect')
    assert len(failed_provider.calls) == 1 and fresh.continuation_calls == 1
    assert runtime.submit_turn(request).replayed
    assert len(failed_provider.calls) == 1


def test_mcp_child_interruption_uses_child_unknown_effect_and_keeps_buffer(tmp_path):
    class Interrupted(SynchronousAIRuntime):
        def _settle_tool_invocation(self, *args, **kwargs):
            if kwargs.get('continuation_state') is not None:
                raise SystemExit('child returned without durable result')
            return super()._settle_tool_invocation(*args, **kwargs)
    runtime, store, request, providers, planner, fresh = mcp_batch(tmp_path, runtime_type=Interrupted)
    waiting = runtime.submit_turn(request)
    with pytest.raises(SystemExit, match='child returned'):
        runtime.apply_action(_mcp_action(waiting, runtime))
    from core.ai_kernel.recovery import classify_recovery
    events = tuple(store.events_after(waiting.turn_id))
    assert classify_recovery(waiting.turn_id, 1, events, payload_loader=store.get).disposition == 'quarantine'
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry, events=store,
        payloads=store, state=store, effect_runner=runtime._effect_runner)
    early = _action(waiting.turn_id, len(events), None)
    early['type'] = 'resume'
    early['idempotency_key'] += '-inflight'
    early['action_id'] += '-inflight'
    assert restarted.apply_action(early).status == 'running'
    events = tuple(store.events_after(waiting.turn_id))
    assert not any(event['type'] == 'tool.outcome.recorded' for event in events)
    from core.effect_log import EffectReaper
    EffectReaper(runtime._effect_runner.log).recover_expired(now=2_000_000_000)
    action = _action(waiting.turn_id, len(events), None)
    action['type'] = 'resume'
    result = restarted.apply_action(action)
    assert result.status == 'failed'
    outcomes = [store.get(event['data']['payload_ref']) for event in store.events_after(waiting.turn_id)
                if event['type'] == 'tool.outcome.recorded']
    assert outcomes[0]['effect_certainty'] == 'unknown'
    assert outcomes[1]['status'] == 'completed'
    assert fresh.continuation_calls == 1 and len(providers[1].calls) == 1


@pytest.mark.parametrize('stage', ['before_buffer', 'before_queue'])
def test_batch_return_without_durable_fact_stays_quarantined(tmp_path, stage):
    class Interrupted(SynchronousAIRuntime):
        def _buffer_tool_dispatch(self, *args, **kwargs):
            if stage == 'before_buffer':
                raise SystemExit('no durable batch fact')
            return super()._buffer_tool_dispatch(*args, **kwargs)
    runtime, store, request, providers, _, _ = mcp_batch(tmp_path, runtime_type=Interrupted)
    original = store.get_or_create_immutable_payload
    def persist(turn_id, kind, payload):
        if stage == 'before_queue' and kind.startswith('tool-batch-mcp-wait-'):
            raise SystemExit('no durable batch fact')
        return original(turn_id, kind, payload)
    store.get_or_create_immutable_payload = persist
    with pytest.raises(SystemExit, match='no durable batch fact'):
        runtime.submit_turn(request)
    from core.ai_kernel.recovery import classify_recovery
    events = tuple(store.events_after(request['turn_id']))
    assert classify_recovery(request['turn_id'], 1, events, payload_loader=store.get).disposition == 'quarantine'
    assert len(providers[1].calls) == 1


@pytest.mark.parametrize('damaged', ['turn_id', 'intent_ref', 'step_id', 'operation_id', 'attempt',
                                    'dispatch_extra', 'unknown', 'missing_buffer', 'missing_start'])
def test_batch_recovery_rejects_damaged_or_unknown_buffer(tmp_path, damaged):
    class Interrupted(SynchronousAIRuntime):
        def _persist_mcp_waiting(self, *args, **kwargs):
            raise SystemExit('queued before waiting event')
    runtime, store, request, providers, _, _ = mcp_batch(tmp_path, runtime_type=Interrupted)
    with pytest.raises(SystemExit, match='queued before waiting event'):
        runtime.submit_turn(request)
    from copy import deepcopy
    from core.ai_kernel.recovery import classify_recovery
    events = tuple(store.events_after(request['turn_id']))
    call_id = next(event['correlation']['tool_call_id'] for event in events
                   if event['type'] == 'tool.started' and event['data']['capability_id'] == 'batch.b')
    def load(ref):
        value = deepcopy(store.get(ref))
        if ref.endswith(f'tool-batch-buffer-v1-{call_id}'):
            if damaged == 'missing_buffer':
                raise KeyError(ref)
            if damaged == 'dispatch_extra':
                value['dispatch']['unexpected'] = True
            elif damaged == 'unknown':
                value['dispatch'] = {'kind': 'failure', 'error_code': 'synthetic.failure',
                    'effect_certainty': 'unknown', 'provider_started': True,
                    'retry_after_ms': None, 'request_state': None}
            elif damaged != 'missing_start':
                value[damaged] = 2 if damaged == 'attempt' else 'synthetic-mismatch'
        return value
    if damaged == 'missing_start':
        events = tuple(event for event in events if not (event['type'] == 'tool.started'
                       and event['correlation']['tool_call_id'] == call_id))
        events = tuple(dict(event, sequence=index) for index, event in enumerate(events, 1))
    assert classify_recovery(request['turn_id'], 1, events, payload_loader=load).disposition == 'quarantine'
    assert len(providers[1].calls) == 1


def test_batch_preparation_failure_closes_unstarted_claims_without_provider(tmp_path):
    class FailedPreparation(SynchronousAIRuntime):
        def _prepare_tool_invocation(self, intent, *args, **kwargs):
            prepared = super()._prepare_tool_invocation(intent, *args, **kwargs)
            if intent.capability_id == 'batch.b':
                raise ValueError('synthetic preparation failure')
            return prepared
    runtime, store, request, providers, _ = sqlite_batch(tmp_path, runtime_type=FailedPreparation)
    result = runtime.submit_turn(request)
    assert result.status == 'failed'
    assert not any(provider.calls for provider in providers)
    from core.effect_log import EffectState
    events = tuple(store.events_after(result.turn_id))
    intents = [store.get(event['data']['payload_ref']) for event in events if event['type'] == 'tool.intent.recorded']
    for intent in intents:
        effect = runtime._effect_runner.log.get(intent['invocation_id'])
        assert effect is None or effect.state is EffectState.SETTLED_ERR
    outcomes = [store.get(event['data']['payload_ref']) for event in events if event['type'] == 'tool.outcome.recorded']
    assert [item['capability_id'] for item in outcomes] == ['batch.a', 'batch.b', 'batch.c']
    assert all(item['status'] == 'failed' and item['effect_certainty'] == 'confirmed_none' for item in outcomes)
    assert events[-1]['type'] == 'turn.failed'


def test_buffered_nested_wire_receipt_resumes_projection_without_new_wire(tmp_path):
    wires = []
    def callback(request):
        handle = request['execution_context'].take_nested_model_handle(invocation_key='primary', purpose='primary')
        handle.model_call_routed(snapshot_ref=f"crp://session/{request['turn_id']}/model-route/frozen",
            snapshot_revision='a'*64, prompt_cache_scope_identity='b'*64,
            provider='synthetic', model='synthetic', execution_location='remote')
        attempt = handle.begin_model_wire_attempt()
        handle.model_call_started(provider='synthetic', model='synthetic')
        def wire():
            wires.append(request['capability_id'])
            attempt.succeeded(usage={'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3}, cache_observation=None)
        attempt.invoke_wire(wire)
        handle.model_call_completed(usage={'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3})
        handle.finalize(error_code=None)
    class Interrupted(SynchronousAIRuntime):
        def _persist_mcp_waiting(self, *args, **kwargs):
            raise SystemExit('queued before event')
    runtime, store, request, providers, planner, fresh = mcp_batch(tmp_path,
        runtime_type=Interrupted, sibling_callback=callback)
    with pytest.raises(SystemExit, match='queued before event'):
        runtime.submit_turn(request)
    from core.ai_kernel.recovery import classify_recovery
    assert classify_recovery(request['turn_id'], 1, tuple(store.events_after(request['turn_id'])),
                             payload_loader=store.get).disposition == 'safe_resume'
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry, events=store,
        payloads=store, state=store, effect_runner=runtime._effect_runner,
        mcp_continuation_reconnector=runtime._mcp_continuation_reconnector)
    lease = acquire(store, request['turn_id'])
    waiting = restarted.recover_accepted_turn(request['turn_id'], lease)
    result = restarted.apply_action(_mcp_action(waiting, restarted), lease)
    assert result.status == 'completed'
    assert wires == ['batch.b'] and len(providers[1].calls) == 1


@pytest.mark.parametrize('effect', ['write', 'delete'])
def test_batch_irreversible_member_executes_alone_between_read_segments(tmp_path, effect):
    observed = []
    def callback(request):
        observed.append(request['capability_id'])
    runtime, store, request, providers, _ = sqlite_batch(tmp_path,
        approval=('batch.b',), modes={'batch.b': 'write'},
        callbacks=dict.fromkeys(('batch.a', 'batch.b', 'batch.c'), callback))
    original = runtime._registry.resolve('batch.b')[0]
    replacement = ScopedCapabilityRegistry()
    for identity in ('batch.a', 'batch.b', 'batch.c'):
        definition, provider = runtime._registry.resolve(identity)
        if identity == 'batch.b':
            definition = replace(original, mode=effect,
                                 tool_definition=replace(original.tool_definition, effect=effect))
        replacement.register(definition, provider)
    runtime._registry = replacement
    waiting = runtime.submit_turn(request)
    event = tuple(store.events_after(waiting.turn_id))[-1]
    result = runtime.apply_action(_action(waiting.turn_id, waiting.current_sequence, event['event_id']))
    assert result.status == 'completed'
    assert observed == ['batch.a', 'batch.b', 'batch.c']
    starts = [event['data']['capability_id'] for event in store.events_after(result.turn_id) if event['type'] == 'tool.started']
    assert starts == observed


def test_batch_cancel_during_approval_never_reopens_or_dispatches(tmp_path):
    runtime, store, request, providers, _ = sqlite_batch(tmp_path, approval=('batch.b',))
    waiting = runtime.submit_turn(request)
    action = _action(waiting.turn_id, waiting.current_sequence, None)
    action['type'] = 'cancel'
    result = runtime.apply_action(action)
    assert result.status == 'cancelled'
    assert not any(provider.calls for provider in providers)
    assert tuple(store.events_after(result.turn_id))[-1]['type'] == 'turn.cancelled'
    assert runtime.apply_action(action).replayed


def test_batch_restart_after_approval_resolution_uses_frozen_preflight(tmp_path):
    visited = []
    class Boundary:
        def evaluate(self, request, definition, decision):
            visited.append(definition.capability_id)
            return ToolExecutionBoundaryDecision('allow', (), (), 1, False, False, {})
    class Interrupted(SynchronousAIRuntime):
        def _save_batch_approval(self, *args, **kwargs):
            raise SystemExit('after approval resolution')
    runtime, store, request, providers, planner = sqlite_batch(tmp_path, approval=('batch.b',),
        boundary=Boundary(), runtime_type=Interrupted)
    waiting = runtime.submit_turn(request)
    event = tuple(store.events_after(waiting.turn_id))[-1]
    action = _action(waiting.turn_id, waiting.current_sequence, event['event_id'])
    with pytest.raises(SystemExit, match='after approval resolution'):
        runtime.apply_action(action)
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry, events=store,
        payloads=store, state=store, effect_runner=runtime._effect_runner, execution_boundary=Boundary())
    lease = acquire(store, waiting.turn_id)
    result = restarted.recover_accepted_turn(waiting.turn_id, lease)
    assert result.status == 'completed'
    assert visited == ['batch.a', 'batch.b', 'batch.c']
    assert all(len(provider.calls) == 1 for provider in providers)
    assert sum(event['type'] == 'approval.required' for event in store.events_after(waiting.turn_id)) == 1


def test_mcp_batch_reject_after_resolution_crash_drains_without_continuation(tmp_path):
    class Interrupted(SynchronousAIRuntime):
        def _record_cancelled_tool_outcome(self, *args, **kwargs):
            raise SystemExit('rejection durable before outcome')
    runtime, store, request, providers, planner, fresh = mcp_batch(tmp_path, runtime_type=Interrupted)
    waiting = runtime.submit_turn(request)
    action = _mcp_action(waiting, runtime)
    action['type'] = 'mcp_reject'
    with pytest.raises(SystemExit, match='rejection durable before outcome'):
        runtime.apply_action(action)
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry, events=store,
        payloads=store, state=store, effect_runner=runtime._effect_runner)
    lease = acquire(store, waiting.turn_id)
    result = restarted.recover_accepted_turn(waiting.turn_id, lease)
    assert result.status == 'cancelled'
    assert [event['data']['capability_id'] for event in store.events_after(waiting.turn_id)
            if event['type'] == 'tool.outcome.recorded'] == ['calendar.read', 'batch.b']
    assert fresh.continuation_calls == 0 and len(providers[1].calls) == 1


def test_direct_run_resumes_durable_batch_without_replanning_members(tmp_path):
    class Interrupted(SynchronousAIRuntime):
        def _prepare_tool_invocation(self, *args, **kwargs):
            raise SystemExit('before provider dispatch')
    runtime, store, request, providers, planner = sqlite_batch(tmp_path, runtime_type=Interrupted)
    with pytest.raises(SystemExit, match='before provider dispatch'):
        runtime.submit_turn(request)
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=store, payloads=store, state=store, effect_runner=runtime._effect_runner)
    result = restarted.run_accepted_turn(request['turn_id'])
    assert result.status == 'completed'
    assert [len(provider.calls) for provider in providers] == [1, 1, 1]
    assert planner.calls == 2
    assert tuple(store.events_after(result.turn_id))[-1]['type'] == 'turn.completed'


def test_original_approve_action_replay_restores_durable_batch_approval(tmp_path):
    visited = []
    class Boundary:
        def evaluate(self, request, definition, decision):
            visited.append(definition.capability_id)
            return ToolExecutionBoundaryDecision('allow', (), (), 1, False, False, {})
    class Interrupted(SynchronousAIRuntime):
        def _save_batch_approval(self, *args, **kwargs):
            raise SystemExit('resolution before approval snapshot')
    runtime, store, request, providers, planner = sqlite_batch(tmp_path,
        approval=('batch.b',), runtime_type=Interrupted, boundary=Boundary())
    waiting = runtime.submit_turn(request)
    event = tuple(store.events_after(waiting.turn_id))[-1]
    action = _action(waiting.turn_id, waiting.current_sequence, event['event_id'])
    with pytest.raises(SystemExit, match='resolution before approval snapshot'):
        runtime.apply_action(action)
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=store, payloads=store, state=store, effect_runner=runtime._effect_runner,
        execution_boundary=Boundary())
    result = restarted.apply_action(action)
    assert result.status == 'completed'
    assert visited == ['batch.a', 'batch.b', 'batch.c']
    assert [len(provider.calls) for provider in providers] == [1, 1, 1]
    assert sum(event['type'] == 'approval.required' for event in store.events_after(result.turn_id)) == 1
    assert restarted.apply_action(action).replayed
    assert [len(provider.calls) for provider in providers] == [1, 1, 1]


def test_mcp_failed_outcome_crash_recovers_sibling_before_turn_terminal(tmp_path):
    class Interrupted(SynchronousAIRuntime):
        def _append(self, turn_id, event_type, *args, **kwargs):
            if event_type == 'tool.failed' and kwargs.get('capability_id') == 'calendar.read':
                raise SystemExit('failure outcome before projection')
            return super()._append(turn_id, event_type, *args, **kwargs)
    runtime, store, request, providers, planner, fresh = mcp_batch(tmp_path, runtime_type=Interrupted)
    def fail(request, request_state):
        fresh.continuation_calls += 1
        assert request_state == b'opaque-request-state'
        raise ToolProviderFailure('synthetic.continuation_failed', effect_certainty='confirmed_none')
    fresh.continue_request_state = fail
    waiting = runtime.submit_turn(request)
    with pytest.raises(SystemExit, match='failure outcome before projection'):
        runtime.apply_action(_mcp_action(waiting, runtime))
    from core.ai_kernel.recovery import classify_recovery
    events = tuple(store.events_after(waiting.turn_id))
    assert classify_recovery(waiting.turn_id, 1, events, payload_loader=store.get).disposition == 'safe_resume'
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=store, payloads=store, state=store, effect_runner=runtime._effect_runner)
    result = restarted.recover_accepted_turn(waiting.turn_id, acquire(store, waiting.turn_id))
    assert result.status == 'failed'
    events = tuple(store.events_after(result.turn_id))
    assert events[-1]['type'] == 'turn.failed'
    assert [event['data']['capability_id'] for event in events if event['type'] == 'tool.outcome.recorded'] == ['calendar.read', 'batch.b']
    assert fresh.continuation_calls == 1 and len(providers[1].calls) == 1


def test_buffered_confirmed_none_retry_is_consumed_once_before_core_retry(tmp_path):
    attempts = []
    def once(request):
        attempts.append(request['attempt'])
        if len(attempts) == 1:
            raise ToolProviderFailure('temporarily_unavailable', effect_certainty='confirmed_none')
    runtime, store, request, providers, planner, fresh = mcp_batch(tmp_path, sibling_callback=once)
    waiting = runtime.submit_turn(request)
    paused = runtime.apply_action(_mcp_action(waiting, runtime))
    assert paused.status == 'running'
    assert attempts == [1]
    from core.effect_log import EffectReaper
    EffectReaper(runtime._effect_runner.log).recover_expired(now=2_000_000_000)
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=store, payloads=store, state=store, effect_runner=runtime._effect_runner)
    events = tuple(store.events_after(paused.turn_id))
    action = _action(paused.turn_id, len(events), None)
    action['type'] = 'resume'
    result = restarted.apply_action(action)
    assert result.status == 'completed'
    assert attempts == [1, 2]
    assert sum(event['type'] == 'tool.attempt.failed' for event in store.events_after(result.turn_id)) == 1
    assert fresh.continuation_calls == 1
    assert restarted.apply_action(action).replayed
    assert attempts == [1, 2]


@pytest.mark.parametrize('mode', ['read', 'draft'])
def test_buffered_invalid_utf8_mcp_state_preserves_failure_and_order(tmp_path, mode):
    def invalid(request):
        raise ToolProviderFailure('mcp.input_required', effect_certainty='unknown', continuation_state=b'\xff')
    runtime, store, request, providers, planner, fresh = mcp_batch(tmp_path,
        sibling_mode=mode, sibling_callback=invalid)
    waiting = runtime.submit_turn(request)
    assert waiting.status == 'waiting_approval'
    result = runtime.apply_action(_mcp_action(waiting, runtime))
    assert result.status == 'failed'
    events = tuple(store.events_after(result.turn_id))
    assert events[-1]['type'] == 'turn.failed'
    outcomes = [store.get(event['data']['payload_ref']) for event in events if event['type'] == 'tool.outcome.recorded']
    assert [item['capability_id'] for item in outcomes] == ['calendar.read', 'batch.b']
    assert outcomes[1]['effect_certainty'] == ('confirmed_none' if mode == 'read' else 'unknown')
    assert outcomes[1]['error_code'] == ('mcp.continuation_invalid' if mode == 'read' else 'ai.tool_outcome_unknown')
    assert fresh.continuation_calls == 1 and len(providers[1].calls) == 1


@pytest.mark.parametrize('entry', ['run', 'recover', 'action_replay'])
def test_mcp_reject_resolution_crash_preserves_rejection_before_cancel_marker(tmp_path, entry):
    class Interrupted(SynchronousAIRuntime):
        def _append(self, turn_id, event_type, *args, **kwargs):
            event = super()._append(turn_id, event_type, *args, **kwargs)
            if event_type == 'approval.resolved' and args[1] == 'mcp_reject':
                raise SystemExit('rejection resolved before cancel marker')
            return event
    runtime, store, request, providers, planner, fresh = mcp_batch(tmp_path, runtime_type=Interrupted)
    waiting = runtime.submit_turn(request)
    action = _mcp_action(waiting, runtime)
    action['type'] = 'mcp_reject'
    with pytest.raises(SystemExit, match='rejection resolved before cancel marker'):
        runtime.apply_action(action)
    before = tuple(store.events_after(waiting.turn_id))
    assert before[-1]['type'] == 'approval.resolved'
    assert not any(event['type'] == 'turn.cancel.requested' for event in before)
    from core.ai_kernel.recovery import classify_recovery
    assert classify_recovery(waiting.turn_id, 1, before, payload_loader=store.get).disposition == 'safe_resume'
    restarted_store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    restarted_effects = EffectRunner(EffectLog(tmp_path / 'effects.sqlite3'),
        owner_id='restarted-mcp-reject', lease_seconds=30)
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=restarted_store, payloads=restarted_store, state=restarted_store,
        effect_runner=restarted_effects)
    lease = acquire(restarted_store, waiting.turn_id)
    if entry == 'run':
        result = restarted.run_accepted_turn(waiting.turn_id, lease)
    elif entry == 'recover':
        result = restarted.recover_accepted_turn(waiting.turn_id, lease)
    else:
        result = restarted.apply_action(action, lease)
    assert result.status == 'cancelled'
    events = tuple(restarted_store.events_after(waiting.turn_id))
    assert events[-1]['type'] == 'turn.cancelled'
    assert sum(event['type'] == 'turn.cancelled' for event in events) == 1
    assert sum(event['type'] == 'mcp.continuation.required' for event in events) == 1
    outcomes = [restarted_store.get(event['data']['payload_ref']) for event in events
                if event['type'] == 'tool.outcome.recorded']
    assert [(item['capability_id'], item['status']) for item in outcomes] == [
        ('calendar.read', 'cancelled'), ('batch.b', 'completed')]
    assert outcomes[0]['error_code'] == 'mcp.continuation_rejected'
    for kind in ('tool.intent.recorded', 'tool.started', 'model.requested', 'model.attempt.dispatched'):
        assert [event['event_id'] for event in events if event['type'] == kind] == [
            event['event_id'] for event in before if event['type'] == kind]
    assert fresh.continuation_calls == 0 and len(providers[1].calls) == 1
    assert restarted.apply_action(action, lease).replayed
    assert restarted.run_accepted_turn(waiting.turn_id, lease).replayed
    assert sum(event['type'] == 'turn.cancelled' for event in restarted_store.events_after(waiting.turn_id)) == 1


@pytest.mark.parametrize('entry', ['run', 'recover'])
def test_saved_boundary_denial_survives_crash_before_preflight_raise(tmp_path, entry):
    visited, current = [], []
    class Boundary:
        def evaluate(self, request, definition, decision):
            visited.append(definition.capability_id)
            return ToolExecutionBoundaryDecision('deny' if definition.capability_id == 'batch.b' else 'allow',
                                                 (), (), 1, False, False, {})
    class NowAllowed:
        def evaluate(self, request, definition, decision):
            current.append(definition.capability_id)
            return ToolExecutionBoundaryDecision('allow', (), (), 2, False, False, {})
    class Interrupted(SynchronousAIRuntime):
        def _append(self, turn_id, event_type, *args, **kwargs):
            event = super()._append(turn_id, event_type, *args, **kwargs)
            if event_type == 'tool.requested' and kwargs.get('capability_id') == 'batch.b':
                raise SystemExit('denied request durable before raise')
            return event
    runtime, store, request, providers, planner = sqlite_batch(tmp_path,
        runtime_type=Interrupted, boundary=Boundary())
    with pytest.raises(SystemExit, match='denied request durable before raise'):
        runtime.submit_turn(request)
    before = tuple(store.events_after(request['turn_id']))
    assert before[-1]['type'] == 'tool.requested'
    assert visited == ['batch.a', 'batch.b']
    call_id = before[-1]['correlation']['tool_call_id']
    frozen = store.get_immutable_payload(request['turn_id'], f'tool-batch-preflight-v1-{call_id}')
    assert frozen[1]['boundary']['outcome'] == 'deny'
    restarted_store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    restarted = SynchronousAIRuntime(planner=planner, registry=runtime._registry,
        events=restarted_store, payloads=restarted_store, state=restarted_store,
        effect_runner=runtime._effect_runner, execution_boundary=NowAllowed())
    lease = acquire(restarted_store, request['turn_id'])
    result = (restarted.run_accepted_turn(request['turn_id'], lease) if entry == 'run' else
              restarted.recover_accepted_turn(request['turn_id'], lease))
    assert result.status == 'failed'
    assert not any(provider.calls for provider in providers)
    assert current == []
    events = tuple(restarted_store.events_after(result.turn_id))
    assert events[-1]['type'] == 'turn.failed'
    assert sum(event['type'] == 'turn.failed' for event in events) == 1
    assert not any(event['type'] in {'tool.intent.recorded', 'tool.started'} for event in events)
    assert restarted_store.get_immutable_payload(result.turn_id, f'tool-batch-preflight-v1-{call_id}') == frozen
    batch = restarted._payloads.get_immutable_payload(result.turn_id,
        f"tool-batch-v1-{before[-1]['correlation']['step_id']}")[1]
    for member in batch['calls']:
        with pytest.raises(KeyError):
            runtime._effect_runner.log.get(member['_execution']['tool_call_id'])
    assert restarted.run_accepted_turn(result.turn_id, lease).replayed
