"""The real HTTP and deferred SSE paths freeze their first decision selection."""
import asyncio

import pytest
from fastapi import HTTPException

from backend.memory_app.v2.policies import ACTIVE, get, override, register, version
from backend.memory_app.v2.policies.pipelines import interfaces_for_turn
from tests.memory_app.v2.kernel_receipts import requests
from tests.memory_app.v2.test_workbench_ask import env as ask_env, publish
from tests.memory_app.v2.test_workbench_remember import env as remember_env, post, wait
from tests.memory_app.v2.test_workbench_stream import events

SEEN = []
CHANGE = lambda: None


@pytest.fixture(scope='module', autouse=True)
def variants():
    for name in ('route', 'place'):
        original = get(name, version='@1')
        for selected in ('@9811', '@9812'):
            def implementation(*args, _name=name, _original=original, _selected=selected, **kwargs):
                SEEN.append((_name, _selected))
                CHANGE()
                return _original(*args, **kwargs)
            register(name, selected)(implementation)


@pytest.fixture
def switch_active(monkeypatch):
    SEEN.clear()
    for name in ('route', 'place'):
        monkeypatch.setitem(ACTIVE, name, '@9811')
    def change():
        for name in ('route', 'place'):
            monkeypatch.setitem(ACTIVE, name, '@9812')
    monkeypatch.setattr(__name__ + '.CHANGE', change)


def test_real_http_remember_spans_route_place_await_and_background_freezes(remember_env, switch_active):
    env = remember_env
    turn = wait(env, post(env))
    assert turn['receipt']['remember']['state'] == 'done'
    frozen = requests(env.records)
    assert frozen and {value['desired_outcome'] for value in frozen} == {'memory.propose_insights'}
    # Organizing uses its separate existing durable store; the background
    # extraction Turn proves that the original request context crossed await.
    for value in frozen:
        assert value['policy_versions']['route'] == '@9811'
        assert value['policy_versions']['place'] == '@9811'
        assert set(value['policy_versions']) == set(interfaces_for_turn(value['desired_outcome']))
    assert len([entry for entry in SEEN if entry[0] == 'route']) == 3
    assert ('place', '@9811') in SEEN
    assert all(selected == '@9811' for _, selected in SEEN)
    assert version('route') == '@9812' and version('place') == '@9812'


def test_real_deferred_sse_ask_uses_first_route_and_place_map(ask_env, switch_active):
    env = ask_env
    publish(env)
    response = env.http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
        headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'policy-stream-first'})
    assert response.status_code == 200, response.text
    parts = events(response)
    assert parts[0][0] == 'started' and parts[-1][0] == 'done'
    result = parts[-1][1]
    assert result['turn']['receipt']['ask']['answer'] == 'Synthetic answer'
    store = env.domains.query.answer_turns.application.state.ai_turn_store
    value = store.get_request(result['turn']['id'])
    assert value['policy_versions']['route'] == '@9811'
    assert value['policy_versions']['place'] == '@9811'
    assert set(value['policy_versions']) == set(interfaces_for_turn('project.answer'))
    assert len([entry for entry in SEEN if entry[0] == 'route']) == 3
    assert ('place', '@9811') in SEEN
    assert all(selected == '@9811' for _, selected in SEEN)
    calls = env.model.calls
    replay = env.http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
        headers={'Accept': 'application/json', 'Idempotency-Key': 'policy-stream-first'})
    assert replay.json() == result and env.model.calls == calls
    assert version('route') == '@9812' and version('place') == '@9812'


def test_direct_execution_exception_restores_callers_context(remember_env, monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPException(409, 'synthetic policy failure')
    register('place', '@9813')(fail)
    async def run():
        with override(place='@9813', route='@9812'):
            with pytest.raises(HTTPException, match='synthetic policy failure'):
                await remember_env.app.state.workbench_turn_execution.run(
                    {'project_id': 'alpha', 'text': '原文证据', 'intent': 'remember'}, 'policy-fail')
            assert version('place') == '@9813' and version('route') == '@9812'
    asyncio.run(run())


def test_explicit_intent_avoids_an_unused_rule_decision(remember_env, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('explicit intent must not run its default expression')
    register('route', '@9814')(fail)
    monkeypatch.setitem(ACTIVE, 'route', '@9814')
    result = post(remember_env, intent='inspiration')
    assert result['turn']['intent'] == 'inspiration' and remember_env.model.calls == 0


def test_completed_sse_replay_uses_saved_intent_after_route_changes(ask_env, monkeypatch):
    env = ask_env
    publish(env)
    body = {'project_id': 'alpha', 'text': 'alpha?'}
    headers = {'Accept': 'text/event-stream', 'Idempotency-Key': 'policy-stream-cache'}
    first = env.http.post('/api/v2/workbench/turns', json=body, headers=headers)
    assert first.status_code == 200 and events(first)[-1][0] == 'done'
    result = events(first)[-1][1]
    calls = env.model.calls
    store = env.domains.query.answer_turns.application.state.ai_turn_store
    frozen = store.get_request(result['turn']['id'])
    seen = []
    def remember(*args, **kwargs):
        seen.append('new route')
        return 'remember'
    register('route', '@9851')(remember)
    monkeypatch.setitem(ACTIVE, 'route', '@9851')
    replay = env.http.post('/api/v2/workbench/turns', json=body, headers=headers)
    assert replay.status_code == 200, replay.text
    assert events(replay)[-1] == ('done', result)
    assert seen == [] and env.model.calls == calls
    assert store.get_request(result['turn']['id']) == frozen


def test_completed_lookup_does_not_bypass_conflict_running_or_unknown_key(ask_env, monkeypatch):
    env = ask_env
    publish(env)
    body = {'project_id': 'alpha', 'text': 'alpha?'}
    key = 'policy-cache-guards'
    first = env.http.post('/api/v2/workbench/turns', json=body, headers={'Idempotency-Key': key})
    assert first.status_code == 200
    conflict = env.http.post('/api/v2/workbench/turns', json={**body, 'text': 'changed?'},
        headers={'Idempotency-Key': key})
    assert conflict.status_code == 409 and conflict.json()['detail'] == 'idempotency_key_conflict'
    from backend.memory_app.v2.turn_execution import REQUESTS
    row = env.records.read(REQUESTS, key)
    with env.records.begin() as tx:
        tx.put(REQUESTS, key, {**row.payload, 'state': 'running'}, expected_revision=row.revision)
        tx.commit()
    running = env.http.post('/api/v2/workbench/turns', json=body, headers={'Idempotency-Key': key})
    assert running.status_code == 409 and running.json()['detail'] == 'turn_in_progress'
    seen = []
    def inspiration(*args, **kwargs):
        seen.append('new route')
        return 'inspiration'
    register('route', '@9852')(inspiration)
    monkeypatch.setitem(ACTIVE, 'route', '@9852')
    unknown = env.http.post('/api/v2/workbench/turns', json=body,
        headers={'Idempotency-Key': 'policy-unknown-cache'})
    assert unknown.status_code == 200 and unknown.json()['turn']['intent'] == 'inspiration'
    assert seen == ['new route'] * 3
    invalid = env.http.post('/api/v2/workbench/turns', json=body,
        headers={'Idempotency-Key': 'invalid key'})
    assert invalid.status_code == 400 and invalid.json()['detail'] == 'invalid_idempotency_key'
