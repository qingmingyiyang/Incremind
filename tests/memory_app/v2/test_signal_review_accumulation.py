"""Count explicit review decisions through original HTTP and accumulation owners."""
import pytest

from backend.memory_app.v2.learning_events import checkpoint, events
from backend.memory_app.v2.signal_reviews import install_signal_review_routes
from backend.memory_app.v2.signals import SignalService
from backend.memory_app.transaction_records import TransactionRecords
from tests.memory_app.v2.test_signal_reviews import real_owner, action
from tests.memory_app.v2.test_workbench_ask import env as ask_env, publish, ask


def install(env):
    owner = real_owner(env)
    install_signal_review_routes(env.http.app, records=env.records, owner=owner)
    return owner


def reviews(env):
    response = env.http.get('/api/v2/library/signal-reviews', params={'project_id': 'alpha'})
    assert response.status_code == 200, response.text
    return response.json()['items']


def choose(env, item, *, decision='confirm'):
    response = env.http.post('/api/v2/library/signal-reviews/decide',
        json={'project_id': 'alpha', 'items': [action(item, decision)]})
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize('kind', ['reask', 'stop'])
def test_actual_ask_and_explicit_decision_count_once_without_changing_fact(ask_env, kind):
    env = ask_env
    publish(env)
    baseline = checkpoint(env.records, 0)['alpha']['score']
    before = events(env.records)['alpha']
    env.model.numbers = []
    answers = [ask(env), ask(env)]
    assert all(response.status_code == 200 for response in answers)
    assert env.model.calls == 2
    install(env)
    if kind == 'stop':
        # Future T14.11 producer contract only; both ASK owners are actual.
        signals = SignalService(env.records)
        signals.record(signals.prepare({'project_id': 'alpha', 'kind': 'stop',
            'client_id': 'future-stop-contract', 'turn_id': answers[0].json()['turn']['id']}, server=True))
    item = next(item for item in reviews(env) if item['kind'] == kind)
    assert events(env.records)['alpha'] == before
    choose(env, item)
    fact, = env.records.list('v2_signal_decisions')
    assert set(fact.payload) == {'review_key', 'kind', 'action', 'turn_ids', 'object', 'at', 'by'}
    assert events(env.records)['alpha'] - before == {'signal-decision:' + fact.object_id}
    state = checkpoint(env.records, 0)['alpha']
    assert state['score'] == baseline + 1
    row = env.records.read('v2_learning_accumulation', 'alpha')
    assert checkpoint(env.records, 0)['alpha'] == state
    assert env.records.read('v2_learning_accumulation', 'alpha') == row
    assert env.records.read('v2_signal_decisions', fact.object_id) == fact
    SignalService(env.records).set_enabled(False, expected_revision=0)
    assert events(env.records)['alpha'] - before == {'signal-decision:' + fact.object_id}
    assert checkpoint(env.records, 0)['alpha'] == state
    assert env.model.calls == 2


@pytest.mark.parametrize('kind,decision', [('reask', 'dismiss'), ('unused', 'confirm')])
def test_actual_dismissal_or_cooling_never_adds_correction_points(ask_env, kind, decision):
    env = ask_env
    publish(env)
    baseline = checkpoint(env.records, 0)['alpha']
    before = events(env.records)['alpha']
    env.model.numbers = []
    for _ in range(5):
        assert ask(env).status_code == 200
    assert env.model.calls == 5
    install(env)
    item = next(item for item in reviews(env) if item['kind'] == kind)
    choose(env, item, decision=decision)
    assert len(env.records.list('v2_signal_decisions')) == 1
    assert events(env.records)['alpha'] == before
    assert checkpoint(env.records, 0)['alpha'] == baseline
    assert env.model.calls == 5


def test_unknown_decision_binding_and_changed_turn_scope_cannot_count(ask_env):
    env = ask_env
    publish(env)
    before = events(env.records)['alpha']
    env.model.numbers = []
    assert ask(env).status_code == ask(env).status_code == 200
    install(env)
    choose(env, next(item for item in reviews(env) if item['kind'] == 'reask'))
    fact, = env.records.list('v2_signal_decisions')
    assert events(env.records)['alpha'] - before == {'signal-decision:' + fact.object_id}
    for changes in ({'kind': []}, {'by': 'admin'}, {'turn_ids': fact.payload['turn_ids'][:1]},
                    {'at': '2026-10-06T12:00:00'}, {'object': {'id': 'wrong'}},
                    {'review_key': '["other-project","reask",[]]'}, {'text': 'unknown extra field'}):
        with env.records.begin() as tx:
            tx.put('v2_signal_decisions', fact.object_id, {**fact.payload, **changes},
                expected_revision=fact.revision)
            assert events(TransactionRecords(tx))['alpha'] == before, changes
    turn = env.records.read('v2_turns', fact.payload['turn_ids'][0])
    with env.records.begin() as tx:
        tx.put('v2_turns', turn.object_id, {**turn.payload, 'project_id': 'other-project'},
            expected_revision=turn.revision)
        assert events(TransactionRecords(tx))['alpha'] == before
        assert 'other-project' not in events(TransactionRecords(tx))
    assert env.records.read('v2_signal_decisions', fact.object_id) == fact
    assert env.records.read('v2_turns', turn.object_id) == turn
    assert env.model.calls == 2
