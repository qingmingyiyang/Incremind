"""Auxiliary background reads retain the real Main/fast routing authorities."""
import json
import sqlite3
from copy import deepcopy
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError, ProviderStoreActivation
from backend.memory_app.kernel.aux_routing import CHOICE_KIND, auxiliary_route
from backend.memory_app.kernel.provider_store_binding import BINDINGS
from backend.memory_app.turn_routing import SNAPSHOT_KIND, RecognitionRoutingSnapshot, _revision
from backend.recognition import WorkScope
from backend.shared.llm.openai_responses import ResponsesCompletion
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.test_main_provider_background import (
    application, env, facts, submit, toggle,
)


def ask_aux(env, http, kind):
    if kind == 'followup':
        _, first = submit(env, http, 'ask')
        env.provider.calls.clear()
        response = http.post('/api/v2/workbench/turns', json={
            'project_id': 'project-a', 'intent': 'ask', 'text': '它有哪些原则？',
            'thread_id': first['thread_id']})
    else:
        response = http.post('/api/v2/workbench/turns', json={
            'project_id': 'project-a', 'intent': 'ask', 'text': 'unmatched request'})
    assert response.status_code == 200, response.text
    result = response.json()
    return result['turn']['id'], result


@pytest.mark.parametrize('kind', ['followup', 'multi-query'])
@pytest.mark.parametrize('phase', ['before_output', 'body'])
def test_aux_and_main_use_their_own_frozen_model_and_physical_attempt(env, kind, phase):
    env.provider.phase = phase
    toggle(env)
    choose(env.models, 'quick')
    env.models.update_model_prices('generation', {'input_per_million': '2',
        'output_per_million': '8', 'cache_read_per_million': '0.04'},
        expected_revision=0, expected_configuration_revision=1)
    with TestClient(application(env)) as http:
        identity, result = ask_aux(env, http, kind)
        posts = [call for call in env.provider.calls if call['method'] == 'POST']
        assert [(call['kind'], call['body']['model']) for call in posts] == [
            ('aux', 'quick'), ('answer', 'main')]
        assert all(call['body'].get('background') is True and call['body'].get('store') is True
                   and call['body']['stream'] is True for call in posts)
        gets = [call for call in env.provider.calls if call['method'] == 'GET']
        assert len(gets) == 2
        assert len({call['identity'] for call in gets}) == 2
        assert [call['after'] for call in gets] == [0 if phase == 'before_output' else 1] * 2
        dispatches, terminals, checkpoints, effects = facts(env, identity)
        assert [row['model_id'] for row in dispatches] == ['quick', 'main']
        store = env.application.state.ai_turn_store
        receipts = [store.get(event['data']['receipt_ref']) for event in store.events_after(identity)
                    if event['type'] == 'model.completed']
        assert [(row['model_call_purpose'], row['model_id']) for row in receipts] == [
            ('aux', 'quick'), ('primary', 'main')]
        assert len(terminals) == 2 and all(row['status'] == 'succeeded' for row in terminals)
        assert all(row['usage'] == {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}
                   for row in terminals)
        assert all(effects[row['attempt_id']] == 'SETTLED_OK' for row in dispatches)
        sequences = [0, 1, 2] if phase == 'before_output' else [0, 1, 2, 3]
        for dispatch, get in zip(dispatches, gets):
            saved = [row for row in checkpoints if row['dispatch'] == dispatch]
            assert [row['cursor'] for row in saved] == [
                {'response_id': get['identity'], 'sequence_number': number} for number in sequences]
            assert all(set(row) == {'schema_version', 'dispatch', 'dispatch_ref', 'cursor',
                                  'previous_ref', 'run_lease', 'effect_lease'} for row in saved)
        main_binding = env.records.read(BINDINGS, identity)
        assert main_binding.payload['auxiliary'] is None
        assert main_binding.payload['configuration']['model'] == 'main'
        choice = env.application.state.ai_turn_store.get_immutable_payload(identity, CHOICE_KIND)
        assert choice[1]['model'] == 'quick'
        receipt = result['turn']['receipt']['ask']
        assert receipt['answer'] == 'Synthetic answer'
        assert [row['id'] for row in receipt['citations']] == [env.source.id]
        prices = [row.payload for row in env.records.list('v2_model_wire_prices')
                  if row.payload['turn_id'] == identity]
        assert len(prices) == 2
        assert [row['rates'] for row in prices if row['model_id'] == 'quick'] == [None]
        assert [row['rates']['input_per_million'] for row in prices if row['model_id'] == 'main'] == ['2']
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


def aux_activation(env, identity):
    from backend.memory_app.kernel.provider_store_binding import auxiliary_provider_store_activation
    store = env.application.state.ai_turn_store
    saved = store.get_immutable_payload(identity, SNAPSHOT_KIND)
    primary = RecognitionRoutingSnapshot(saved[0], _revision(saved[1]), saved[1]).generation_binding()
    selected, route = auxiliary_route(env.models, store, identity, 'project-a', primary)
    activation = auxiliary_provider_store_activation(env.models, selected, env.records, store,
        store.get_request(identity), saved[1], route)
    return activation, selected, route


def test_unconfigured_fast_aux_uses_the_frozen_main_model_without_a_new_choice(env):
    toggle(env)
    with TestClient(application(env)) as http:
        identity, result = ask_aux(env, http, 'multi-query')
        posts = [call for call in env.provider.calls if call['method'] == 'POST']
        assert [(call['kind'], call['body']['model']) for call in posts] == [('aux', 'main'), ('answer', 'main')]
        assert all(call['body']['background'] is True for call in posts)
        assert len([call for call in env.provider.calls if call['method'] == 'GET']) == 2
        activation, selected, _route = aux_activation(env, identity)
        assert selected is env.models and activation.expected_purpose == 'aux'
        assert result['turn']['receipt']['ask']['answer'] == 'Synthetic answer'
        assert env.application.state.ai_turn_store.get_immutable_payload(identity, CHOICE_KIND)[1]['model'] is None


@pytest.mark.parametrize('unsupported', ['off', 'undeclared'])
def test_aux_off_or_undeclared_adapter_never_adopts_background(env, unsupported):
    choose(env.models, 'quick')
    if unsupported == 'undeclared':
        toggle(env)
        env.models._completion_fn = ResponsesCompletion(api_base=env.provider.base)
    with TestClient(application(env)) as http:
        identity, result = ask_aux(env, http, 'multi-query')
        posts = [call for call in env.provider.calls if call['method'] == 'POST']
        assert [(call['kind'], call['body']['model']) for call in posts] == [('aux', 'quick'), ('answer', 'main')]
        assert all('background' not in call['body'] and 'store' not in call['body'] for call in posts)
        assert not any(call['method'] == 'GET' for call in env.provider.calls)
        assert env.records.read(BINDINGS, identity).payload['enabled'] is False
        assert aux_activation(env, identity)[0] is None
        assert result['turn']['receipt']['ask']['answer'] == 'Synthetic answer'


def test_real_do_preserves_worker_off_while_steward_and_verified_main_are_on(env):
    toggle(env)
    choose(env.models, 'quick')
    with TestClient(application(env)) as http:
        identity, _result = submit(env, http, 'do')
        posts = [call for call in env.provider.calls if call['method'] == 'POST']
        assert [(call['kind'], call['body']['model']) for call in posts] == [
            ('steward', 'main'), ('worker', 'main'), ('main', 'main')]
        assert posts[0]['body']['store'] is True and posts[0]['body']['background'] is True
        assert 'store' not in posts[1]['body'] and 'background' not in posts[1]['body']
        assert posts[-1]['body']['store'] is True and posts[-1]['body']['background'] is True
        assert sum(call['method'] == 'GET' for call in env.provider.calls) == 2
        composition, store = env.application.state.agent_runtime_composition, env.application.state.ai_turn_store
        children = [run for run in composition.store.list_runs(project_id='project-a') if run.role == 'subagent']
        assert sorted(run.profile_id for run in children) == ['steward.scheduler', 'subagent.worker']
        for child in children:
            assert store.events_after(child.turn_id)[-1]['type'] == 'turn.completed'
            binding = env.records.read(BINDINGS, child.turn_id)
            if child.profile_id == 'steward.scheduler':
                assert binding.payload['enabled'] is True and binding.payload['auxiliary'] is None
                assert binding.payload['configuration']['model'] == 'main'
            else:
                assert binding is None
        worker = next(child for child in children if child.profile_id == 'subagent.worker')
        events = store.events_after(worker.turn_id)
        assert sum(event['type'] == 'tool.intent.recorded' for event in events) == 1
        assert sum(event['type'] == 'tool.completed' for event in events) == 1
        assert store.events_after(identity)[-1]['type'] == 'turn.completed'


@pytest.mark.parametrize('revocation', ['fast', 'store', 'source', 'cancel'])
def test_aux_each_get_rechecks_the_original_choice_and_live_authority(env, revocation):
    from backend.memory_app.source_egress import SourceEgressService
    from core.ai_kernel.dispatcher import ToolDispatchCancelled
    toggle(env)
    choose(env.models, 'quick')
    application(env)
    previous = set()
    def revoke():
        rows = [row for row in env.records.list(BINDINGS) if row.object_id not in previous]
        assert len(rows) == 1
        identity = rows[0].object_id
        deadline = monotonic() + 2
        while monotonic() < deadline and not facts(env, identity)[2]:
            sleep(.01)
        assert facts(env, identity)[2]
        if revocation == 'fast':
            choose(env.models, 'other', 1)
        elif revocation == 'store':
            toggle(env, False)
        elif revocation == 'source':
            SourceEgressService(env.records).set_policy(WorkScope('local-user', 'project-a'),
                'recognition', env.source.id, env.source.revision, 0, [])
        else:
            assert env.application.state.ai_turn_runner.request_turn_cancel(identity,
                reason='Synthetic aux cancellation') is True
    with TestClient(env.application) as http:
        body = {'project_id': 'project-a', 'intent': 'ask', 'text': 'unmatched request'}
        if revocation == 'source':
            first_id, first = submit(env, http, 'ask')
            first_ask = first['turn']['receipt']['ask']
            assert [citation['id'] for citation in first_ask['citations']] == [env.source.id]
            evidence = env.records.read('workspace_ask_receipts', first_ask['egress_receipt_id'])
            assert [(source['kind'], source['id'], source['revision']) for source in evidence.payload['sources']] == [
                ('recognition', env.source.id, env.source.revision)]
            previous.add(first_id)
            env.provider.calls.clear()
            body.update(text='它有哪些原则？', thread_id=first['thread_id'])
        env.provider.after_emitted = revoke
        if revocation == 'cancel':
            with pytest.raises(ToolDispatchCancelled):
                http.post('/api/v2/workbench/turns', json=body)
        else:
            response = http.post('/api/v2/workbench/turns', json=body)
        assert env.provider.callback_errors == []
        posts = [call for call in env.provider.calls if call['method'] == 'POST']
        assert len(posts) == 1 and posts[0]['kind'] == 'aux'
        assert posts[0]['body']['model'] == 'quick' and posts[0]['body']['background'] is True
        assert not any(call['method'] == 'GET' for call in env.provider.calls)
        identity = next(row.object_id for row in env.records.list(BINDINGS) if row.object_id not in previous)
        dispatches, terminals, checkpoints, effects = facts(env, identity)
        assert len(dispatches) == len(terminals) == 1 and checkpoints
        assert dispatches[0]['model_id'] == 'quick' and terminals[0]['status'] != 'succeeded'
        assert terminals[0]['usage'] is None and effects[dispatches[0]['attempt_id']] == 'UNKNOWN'
        if revocation in {'fast', 'store'}:
            assert response.status_code == 200
            ask = response.json()['turn']['receipt']['ask']
            assert ask['no_match'] is True and ask['answer'] == '当前项目中没有匹配且可用于回答的资料。'
            assert ask['citations'] == []
            result = env.application.state.ai_turn_store.get_immutable_payload(identity, 'product-answer-result-v2')[1]
            assert result['receipt']['ask']['no_match'] is True and result['chosen'] == []
        elif revocation == 'source':
            assert response.status_code >= 400
        else:
            assert env.application.state.ai_runtime.receipt_for(identity).status == 'cancelled'
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


@pytest.mark.parametrize('failure', ['observer', 'close'])
def test_aux_observer_or_owned_close_failure_never_repeats_the_post(env, failure):
    from types import SimpleNamespace
    from backend.memory_app.kernel.ai_runtime import get_or_build_ai_runtime
    toggle(env)
    choose(env.models, 'quick')
    application(env)
    if failure == 'observer':
        get_or_build_ai_runtime(SimpleNamespace(app=env.application), SimpleNamespace(root_dir=env.root))
        with sqlite3.connect(env.application.state.ai_turn_store._path) as connection:
            connection.execute("CREATE TRIGGER synthetic_aux_observer BEFORE INSERT ON ai_turn_immutable_payloads "
                "WHEN NEW.kind LIKE 'model-provider-checkpoint-%' BEGIN SELECT RAISE(ABORT,'synthetic aux'); END")
    else:
        env.provider.fail_close = True
    with TestClient(env.application) as http:
        response = http.post('/api/v2/workbench/turns', json={
            'project_id': 'project-a', 'intent': 'ask', 'text': 'unmatched request'})
        assert response.status_code == 200
        ask = response.json()['turn']['receipt']['ask']
        assert ask['no_match'] is True and ask['answer'] == '当前项目中没有匹配且可用于回答的资料。'
        assert ask['citations'] == []
        posts = [call for call in env.provider.calls if call['method'] == 'POST']
        assert len(posts) == 1 and posts[0]['kind'] == 'aux'
        assert not any(call['method'] == 'GET' for call in env.provider.calls)
        identity = env.records.list(BINDINGS)[0].object_id
        result = env.application.state.ai_turn_store.get_immutable_payload(identity, 'product-answer-result-v2')[1]
        assert result['receipt']['ask']['no_match'] is True and result['chosen'] == []
        dispatches, terminals, checkpoints, effects = facts(env, identity)
        assert len(dispatches) == len(terminals) == 1
        assert terminals[0]['status'] != 'succeeded' and terminals[0]['usage'] is None
        assert effects[dispatches[0]['attempt_id']] == 'UNKNOWN'
        if failure == 'observer':
            assert checkpoints == []
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


def frozen_auxiliary(env):
    from core.ai_kernel import SQLiteAITurnStore
    from tests.memory_app.v2.test_main_provider_store_bindings import request, routing, acquire
    toggle(env)
    choose(env.models, 'quick')
    env.store = SQLiteAITurnStore(env.root / 'auxiliary-binding.sqlite3')
    value = request(env)
    primary = acquire(routing(env), value)
    selected, route = auxiliary_route(env.models, env.store, value['turn_id'], 'alpha', primary.generation_binding())
    return value, primary, selected, route


def frozen_activation(env, value, primary, selected, route):
    from backend.memory_app.kernel.provider_store_binding import auxiliary_provider_store_activation
    return auxiliary_provider_store_activation(env.models, selected, env.records, env.store,
        value, primary.payload, route)


@pytest.mark.parametrize('missing', ['main-binding', 'aux-choice'])
def test_missing_auxiliary_history_never_adopts_current_on_selection(env, missing):
    value, primary, selected, route = frozen_auxiliary(env)
    if missing == 'main-binding':
        with env.records.begin() as tx:
            row = tx.read(BINDINGS, value['turn_id'])
            tx.delete(BINDINGS, value['turn_id'], expected_revision=row.revision)
            tx.commit()
    else:
        with sqlite3.connect(env.store._path) as connection:
            connection.execute('DELETE FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?',
                (value['turn_id'], CHOICE_KIND))
    before = env.records.read(BINDINGS, value['turn_id'])
    assert frozen_activation(env, value, primary, selected, route) is None
    assert env.records.read(BINDINGS, value['turn_id']) == before
    assert env.provider.calls == [] and tuple(env.store.events_after(value['turn_id'])) == ()


@pytest.mark.parametrize('drift', ['fast', 'generation', 'mode', 'store', 'adapter'])
def test_live_auxiliary_binding_drift_rejects_before_any_dispatch(env, drift):
    value, primary, selected, route = frozen_auxiliary(env)
    before = env.records.read(BINDINGS, value['turn_id'])
    if drift == 'fast':
        choose(env.models, 'other', 1)
    elif drift == 'generation':
        env.models.update('generation', {'base_url': 'https://synthetic-other.invalid/v1', 'expected_revision': 1})
    elif drift == 'mode':
        with env.records.begin() as tx:
            row = tx.read('recognition_generation_mode', 'default')
            tx.put('recognition_generation_mode', 'default', {**row.payload, 'mode': 'local'}, expected_revision=row.revision)
            tx.commit()
    elif drift == 'store':
        toggle(env, False)
    else:
        env.models._completion_fn = ResponsesCompletion(api_base=env.provider.base)
    with pytest.raises(ModelConfigurationError):
        frozen_activation(env, value, primary, selected, route)
    assert env.records.read(BINDINGS, value['turn_id']) == before
    assert env.provider.calls == [] and tuple(env.store.events_after(value['turn_id'])) == ()


@pytest.mark.parametrize('mismatch', ['revision', 'prompt_cache_scope_identity', 'payload_ref', 'configuration', 'purpose'])
def test_auxiliary_route_identity_mismatch_never_mints_activation(env, mismatch):
    value, primary, selected, route = frozen_auxiliary(env)
    route, value = deepcopy(route), deepcopy(value)
    if mismatch in {'revision', 'prompt_cache_scope_identity'}:
        route[mismatch] = '0' * 64
    elif mismatch == 'payload_ref':
        route['payload_ref'] = 'crp://session/foreign/model-routing'
    elif mismatch == 'configuration':
        route['configuration']['model'] = 'other'
    else:
        value['desired_outcome'] = 'project.task'
    with pytest.raises(ModelConfigurationError):
        frozen_activation(env, value, primary, selected, route)
    assert env.provider.calls == [] and tuple(env.store.events_after(value['turn_id'])) == ()
