"""Material-bound gap calls use real SQLite, product Turns and governed wires."""
import asyncio
from dataclasses import replace
import json
import time
from threading import Event, Thread

import pytest

from backend.memory_app.kernel.answer_turns import ACTIVE_ANSWER, generate_answer, gap_answer_input, answer_observation
from backend.memory_app.v2.followup import auxiliary_call
from backend.memory_app.v2.multi_query import expand_gap_plan
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.model_config import ModelConfigurationError
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import WorkScope
from backend.memory_app.v2.multi_query import QueryVariants
from backend.memory_app.v2.policies import get, override
from tests.memory_app.v2.test_fast_aux_calls import fast_env
from tests.memory_app.v2.test_ladder import document, recognition


BINDING = 'answer-gap-binding-gap-drilldown-v1'
INPUT = 'answer-gap-input-gap-drilldown-v1'


def prepare(env):
    env.query = env.domains.query
    document(env, summary='meridian cobalt', body='telescope aperture 17.4')
    recognition(env, 'meridian applies unless the instrument is retired')
    with override(retrieve='@4'):
        collected = env.query.collect_candidates('alpha', 'meridian telescope aperture observer tolerance?')
        plan = env.query.prepare_drilldown('alpha', 'meridian telescope aperture observer tolerance?', collected=collected)
    assert plan['drilldown_needed'] and {'L3', 'L2'} == {row['layer'] for row in plan['chosen']}
    return plan


def messages(plan):
    return get('retrieve', version='@4')(None, plan['question'],
        [row['excerpt'] for row in plan['chosen']], operation='gap_messages')


def call(env, plan, *, contents=None):
    return generate_answer(env.model, messages(plan) if contents is None else contents,
        response_model=QueryVariants, max_tokens=512, validate_current=lambda: env.query.validate_ask_plan(plan),
        purpose='aux', invocation_key='gap-drilldown', gap_plan=plan)


def run(env, plan, key, operation):
    return asyncio.run(env.query.answer_turns.run(turn_id=key, project='alpha', question=plan['question'],
        operation=operation, policy_versions=plan['policy_versions']))


@pytest.mark.parametrize('tamper', ['excerpt', 'window'])
def test_actual_tampered_material_is_rejected_before_wire(fast_env, tamper):
    env, plan = fast_env, prepare(fast_env)
    row = next(row for row in plan['chosen'] if row['layer'] == 'L2')
    if tamper == 'excerpt':
        row['excerpt'] = 'injected private text'
    else:
        row['windows'] = (replace(row['windows'][0], text='injected private text'),)
        row['excerpt'] = 'injected private text'
    async def operation():
        try:
            call(env, plan)
        except Exception:
            return {'rejected': True}
        return {'rejected': False}
    assert run(env, plan, 'tampered-' + tamper, operation)['rejected'] is True
    assert env.calls == []


def test_actual_wire_sees_committed_material_proof_and_call_identity(fast_env):
    env, plan = fast_env, prepare(fast_env)
    observed = []
    original = env.model._completion_fn
    def transport(**request):
        store = env.app.state.ai_turn_store
        binding = store.get_immutable_payload('bound-gap', BINDING)
        observed.append(binding)
        if binding:
            input_payload = store.get(binding[1]['input_ref'])
            assert input_payload['messages'] == request['messages']
            assert binding[1]['model_request_id'] in {
                event['correlation'].get('model_request_id') for event in store.events_after('bound-gap')
                if event['type'] == 'model.requested'}
            assert input_payload['material_proof']['privacy']['source_snapshots']
            insight = next(row for row in plan['chosen'] if row['layer'] == 'L3')
            assert insight['excerpt'] in request['messages'][-1]['content']
            assert 'unless the instrument is retired' in request['messages'][-1]['content']
            assert any(ref['type'] == 'recognition' and ref['id'] == insight['entry']['id']
                for ref in input_payload['material_proof']['privacy']['material_refs'])
            assert any(node['type'] == 'recognition' and node['id'] == insight['entry']['id']
                for snapshot in input_payload['material_proof']['privacy']['source_snapshots'] for node in snapshot['nodes'])
            active = ACTIVE_ANSWER.get()
            released = []
            def probe():
                lock = active[3].invocation_lock
                acquired = lock.acquire(blocking=False)
                released.append(acquired)
                if acquired:
                    lock.release()
                with env.records.begin() as tx:
                    tx.put('gap_network_probe', 'observed', {'committed': True}, expected_revision=0)
                    tx.commit()
                released.append(True)
            worker = Thread(target=probe)
            worker.start()
            worker.join(timeout=2)
            assert not worker.is_alive() and released == [True, True]
        return original(**request)
    env.model._completion_fn = transport
    async def operation():
        output, _ = call(env, plan)
        return output.model_dump()
    assert run(env, plan, 'bound-gap', operation)['queries']
    assert len(observed) == 1 and observed[0] is not None
    binding = observed[0][1]
    proof = env.app.state.ai_turn_store.get(binding['input_ref'])['material_proof']
    summary = next(row for row in plan['chosen'] if row['layer'] == 'L2')
    assert summary['id'] != summary['entry']['id']
    assert any(ref['id'] == summary['entry']['id'] for ref in proof['privacy']['material_refs'])
    terminal = next(event for event in env.app.state.ai_turn_store.events_after('bound-gap')
        if event['type'] == 'model.completed')
    assert env.app.state.ai_turn_store.get(terminal['data']['receipt_ref'])['model_request_id'] == binding['model_request_id']


def test_actual_failed_invocation_cannot_allocate_another_wire(fast_env):
    env, plan = fast_env, prepare(fast_env)
    attempts = []
    def transport(**request):
        attempts.append(request)
        raise ConnectionError('synthetic transport failed')
    env.model._completion_fn = transport
    async def operation():
        failures = 0
        for _ in range(2):
            try:
                call(env, plan)
            except Exception:
                failures += 1
        return {'failures': failures}
    assert run(env, plan, 'failed-gap', operation) == {'failures': 2}
    assert len(attempts) == 1
    events = env.app.state.ai_turn_store.events_after('failed-gap')
    assert len([event for event in events if event['type'] == 'model.requested']) == 1


def test_actual_cached_gap_compares_input_before_return_and_root_history_stays_readonly(fast_env):
    env, plan = fast_env, prepare(fast_env)
    async def operation():
        first, metadata = call(env, plan)
        second, replay = call(env, plan)
        assert first == second and metadata == replay
        changed = messages(plan)
        changed[-1]['content'] += '\nunauthorized addition'
        with pytest.raises(ValueError, match='gap_messages_changed'):
            call(env, plan, contents=changed)
        return first.model_dump()
    result = run(env, plan, 'cache-gap', operation)
    assert len(env.calls) == 1
    env.model.update('generation', {'allow_remote': False, 'expected_revision': 1})
    async def forbidden():
        raise AssertionError('completed root cache executed a new closure')
    assert run(env, plan, 'cache-gap', forbidden) == result
    assert len(env.calls) == 1


def test_actual_inflight_duplicate_has_only_one_original_handle_and_wire(fast_env):
    env, plan = fast_env, prepare(fast_env)
    entered, release = Event(), Event()
    original = env.model._completion_fn
    def transport(**request):
        entered.set()
        assert release.wait(timeout=3)
        return original(**request)
    env.model._completion_fn = transport
    async def operation():
        first = asyncio.create_task(asyncio.to_thread(call, env, plan))
        assert await asyncio.to_thread(entered.wait, 3)
        try:
            with pytest.raises(ValueError, match='gap_invocation_interrupted'):
                await asyncio.to_thread(call, env, plan)
        finally:
            release.set()
        output, _ = await first
        return output.model_dump()
    assert run(env, plan, 'inflight-gap', operation)['queries']
    assert len(env.calls) == 1
    events = env.app.state.ai_turn_store.events_after('inflight-gap')
    assert len([event for event in events if event['type'] == 'model.requested']) == 1


def test_actual_historical_input_without_binding_is_never_repaired_by_wire(fast_env):
    env, plan = fast_env, prepare(fast_env)
    async def operation():
        _, route, prepared = gap_answer_input(env.model, plan)
        payload = {**prepared, 'purpose': 'aux', 'response_schema': QueryVariants.model_json_schema(),
            'max_tokens': 512, 'route_ref': route['payload_ref'], 'route_revision': route['revision']}
        env.app.state.ai_turn_store.get_or_create_immutable_payload('unbound-gap', INPUT, payload)
        with pytest.raises(ValueError, match='gap_invocation_unbound_or_changed'):
            call(env, plan)
        return {'rejected': True}
    assert run(env, plan, 'unbound-gap', operation)['rejected']
    assert env.calls == []
    assert not any(event['type'] == 'model.requested' for event in env.app.state.ai_turn_store.events_after('unbound-gap'))


def test_actual_privacy_revocation_after_dispatch_retains_paid_wire_and_blocks_replay(fast_env):
    env, plan = fast_env, prepare(fast_env)
    original = env.model._completion_fn
    def transport(**request):
        response = original(**request)
        root = plan['chosen'][0]['snapshot']['roots'][0]
        SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'), root['type'], root['id'],
            root['revision'], 0, [])
        return response
    env.model._completion_fn = transport
    async def operation():
        for _ in range(2):
            with pytest.raises(Exception):
                call(env, plan)
        observation = answer_observation('gap-drilldown')
        assert observation['complete'] and observation['observations'][0]['total_tokens'] == 5
        return {'rejected': True}
    assert run(env, plan, 'revoked-gap', operation)['rejected']
    assert len(env.calls) == 1
    store = env.app.state.ai_turn_store
    assert store.get_immutable_payload('revoked-gap', 'answer-model-result-gap-drilldown') is None
    assert store.get_immutable_payload('revoked-gap', BINDING)


def test_actual_timeout_and_late_transport_never_create_a_second_gap_wire(fast_env):
    env, plan = fast_env, prepare(fast_env)
    release, settled = Event(), Event()
    original = env.model._completion_fn
    def transport(**request):
        try:
            assert release.wait(timeout=15)
            return original(**request)
        finally:
            settled.set()
    env.model._completion_fn = transport
    async def operation():
        result = await auxiliary_call(env.query, 'alpha', None, QueryVariants, turn_id='timeout-gap',
            validate_current=lambda: env.query.validate_ask_plan(plan), gap_plan=plan)
        assert result['status'] == 'timeout' and result['output'] is None
        with pytest.raises(ValueError, match='gap_invocation_interrupted'):
            call(env, plan)
        release.set()
        assert await asyncio.to_thread(settled.wait, 3)
        # Wait for the original governed worker to finish its terminal checks.
        handle = ACTIVE_ANSWER.get()[3]['gap-drilldown']
        deadline = time.monotonic() + 3
        while not handle.discarded and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        assert handle.discarded and handle.terminal == 'failed'
        return {'timed_out': True}
    assert run(env, plan, 'timeout-gap', operation)['timed_out']
    assert len(env.calls) == 1
    store = env.app.state.ai_turn_store
    assert store.get_immutable_payload('timeout-gap', 'answer-model-result-gap-drilldown') is None
    assert len([event for event in store.events_after('timeout-gap') if event['type'] == 'model.requested']) == 1


def test_actual_binding_write_rollback_leaves_one_failed_handle_and_zero_wires(fast_env):
    env, plan = fast_env, prepare(fast_env)
    async def operation():
        store = env.app.state.ai_turn_store
        connection = store._connect()
        try:
            connection.execute("CREATE TRIGGER gap_binding_failure BEFORE INSERT ON ai_turn_immutable_payloads "
                "WHEN NEW.kind='answer-gap-binding-gap-drilldown-v1' BEGIN "
                "SELECT RAISE(ABORT, 'synthetic binding write denied'); END")
        finally:
            connection.close()
        for _ in range(2):
            with pytest.raises(Exception):
                call(env, plan)
        assert store.get_immutable_payload('binding-failed', INPUT)
        assert store.get_immutable_payload('binding-failed', BINDING) is None
        return {'rejected': True}
    assert run(env, plan, 'binding-failed', operation)['rejected']
    assert env.calls == []
    events = env.app.state.ai_turn_store.events_after('binding-failed')
    assert len([event for event in events if event['type'] == 'model.requested']) == 1
    assert not any(event['type'] == 'model.attempt.dispatched' for event in events)


@pytest.mark.parametrize('private', [False, True])
def test_actual_empty_or_private_upper_selection_resumes_without_gap_wire(fast_env, private):
    env = fast_env
    env.query = env.domains.query
    if private:
        document(env, summary='meridian cobalt', body='meridian telescope aperture observer tolerance')
        set_private_project(env.records, 'alpha', True, 0)
    with override(retrieve='@4'):
        collected = env.query.collect_candidates('alpha', 'meridian telescope aperture observer tolerance?')
        plan = env.query.prepare_drilldown('alpha', 'meridian telescope aperture observer tolerance?', collected=collected)
    assert plan['chosen'] == []
    async def operation():
        result, observation, rewrite = await expand_gap_plan(env.query, 'alpha', plan['question'], plan, collected,
            turn_id='empty-' + str(private))
        assert result['chosen'] == [] and observation['status'] == 'skipped'
        assert rewrite == {'queries': [], 'used': False}
        return {'resumed': True}
    assert run(env, plan, 'empty-' + str(private), operation)['resumed']
    assert env.calls == []


def test_actual_bound_gap_output_drives_original_lower_collector(fast_env):
    env = fast_env
    env.query = env.domains.query
    third = document(env, summary='archive inventory', body='deepomega detail calibrated',
        original='deepomega detail original calibration')
    plan = prepare(env)
    collected = plan['_drilldown']['collected']
    original = env.model._completion_fn
    def transport(**request):
        response = original(**request)
        response['choices'][0]['message']['content'] = json.dumps({'queries': ['deepomega detail']})
        return response
    env.model._completion_fn = transport
    async def operation():
        result, observation, rewrite = await expand_gap_plan(env.query, 'alpha', plan['question'], plan, collected,
            turn_id='lower-gap')
        assert observation['status'] == 'completed' and observation['receipt_ids']
        assert rewrite == {'queries': ['deepomega detail'], 'used': True}
        assert any(row.get('document_id') == third and row['layer'] in {'L1', 'L0'} for row in result['chosen'])
        assert result['chosen'][:len(plan['chosen'])] == plan['chosen']
        return {'drilled': True}
    assert run(env, plan, 'lower-gap', operation)['drilled']
    assert len(env.calls) == 1


def test_actual_sufficient_upper_selection_has_no_auxiliary_call(fast_env):
    env, plan = fast_env, prepare(fast_env)
    with override(retrieve='@4'):
        question = 'meridian applies retired?'
        collected = env.query.collect_candidates('alpha', question)
        plan = env.query.prepare_drilldown('alpha', question, collected=collected)
    assert plan['chosen'] and not plan['drilldown_needed']
    async def operation():
        result, observation, rewrite = await expand_gap_plan(env.query, 'alpha', question, plan, collected,
            turn_id='sufficient-gap')
        assert observation['status'] == 'skipped' and not rewrite['used']
        assert result['chosen'] == plan['chosen']
        return {'sufficient': True}
    assert run(env, plan, 'sufficient-gap', operation)['sufficient']
    assert env.calls == []


def test_actual_disabled_remote_gap_preserves_original_authority_rejection(fast_env):
    env, _ = fast_env, prepare(fast_env)
    env.model.update('generation', {'allow_remote': False, 'expected_revision': 1})
    with override(retrieve='@4'):
        question = 'meridian telescope aperture observer tolerance?'
        collected = env.query.collect_candidates('alpha', question)
        plan = env.query.prepare_drilldown('alpha', question, collected=collected)
        expected = env.query.prepare_ask('alpha', question, collected=collected)
    with pytest.raises(ModelConfigurationError, match='^remote_disabled$'):
        env.query.validate_ask_plan(expected)
    async def operation():
        with pytest.raises(ModelConfigurationError, match='^remote_disabled$'):
            await expand_gap_plan(env.query, 'alpha', question, plan, collected, turn_id='disabled-gap')
        return {'blocked': True}
    assert run(env, plan, 'disabled-gap', operation)['blocked']
    assert env.calls == []


def test_actual_failed_gap_resumes_same_budget_and_original_lower_result(fast_env):
    env, plan = fast_env, prepare(fast_env)
    collected = plan['_drilldown']['collected']
    with override(retrieve='@4'):
        expected = env.query.prepare_ask('alpha', plan['question'], collected=collected)
    attempts = []
    def transport(**request):
        attempts.append(request)
        raise ConnectionError('synthetic external failure')
    env.model._completion_fn = transport
    async def operation():
        result, observation, rewrite = await expand_gap_plan(env.query, 'alpha', plan['question'], plan, collected,
            turn_id='fallback-gap')
        assert observation['status'] == 'failed' and observation['receipt_ids']
        assert result['chosen'] == expected['chosen'] and result['trace'] == expected['trace']
        assert result['budget'] == expected['budget'] and not rewrite['used']
        return {'resumed': True}
    assert run(env, plan, 'fallback-gap', operation)['resumed']
    assert len(attempts) == 1


def test_actual_semantically_invalid_gap_keeps_paid_receipt_and_marks_fallback_failed(fast_env):
    env, plan = fast_env, prepare(fast_env)
    collected = plan['_drilldown']['collected']
    with override(retrieve='@4'):
        expected = env.query.prepare_ask('alpha', plan['question'], collected=collected)
    original = env.model._completion_fn
    def transport(**request):
        response = original(**request)
        response['choices'][0]['message']['content'] = json.dumps({'queries': ['x' * 201]})
        return response
    env.model._completion_fn = transport
    async def operation():
        result, observation, rewrite = await expand_gap_plan(env.query, 'alpha', plan['question'], plan, collected,
            turn_id='invalid-gap')
        assert observation['status'] == 'failed' and observation['receipt_ids']
        assert observation['usage']['total_tokens'] == 5
        assert result['chosen'] == expected['chosen'] and result['trace'] == expected['trace']
        assert not rewrite['used']
        return {'resumed': True}
    assert run(env, plan, 'invalid-gap', operation)['resumed']
    assert len(env.calls) == 1
