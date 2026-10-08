"""Original HTTP-to-primary timing with a frozen, sufficient gap policy."""
from statistics import median
import time

from backend.memory_app.v2.policies import ACTIVE, override
from tests.memory_app.v2.test_workbench_ask import env, publish


def test_frozen_v4_sufficient_ask_local_dispatch_latency(env):
    insight, _ = publish(env)
    samples, identities = [], []
    for index in range(5):
        started = time.perf_counter()
        env.model.before = lambda: samples.append((time.perf_counter() - started) * 1000)
        with override(retrieve='@4'):
            response = env.http.post('/api/v2/workbench/turns',
                headers={'Idempotency-Key': f'gap-free-latency-{index}'},
                json={'project_id': 'alpha', 'text': 'alpha beta gamma?'})
        assert response.status_code == 200, response.text
        turn = response.json()['turn']
        identities.append(turn['id'])
        assert len(samples) == index + 1
        store = env.domains.query.answer_turns.application.state.ai_turn_store
        assert store.get_request(turn['id'])['policy_versions']['retrieve'] == '@4'
        receipt = turn['receipt']['ask']
        assert [row['id'] for row in receipt['citations']] == [insight.id]
        assert receipt['trace'][0]['layer'] == 'insight'
        assert receipt['trace'][0]['coverage'] == 1 and receipt['trace'][0]['stopped'] is True
        assert receipt['trace'][0]['rewrite_status'] == 'skipped'
        completed = [store.get(event['data']['receipt_ref']) for event in store.events_after(turn['id'])
            if event['type'] == 'model.completed']
        assert len(completed) == 1 and completed[0]['model_call_purpose'] == 'primary'
        assert len([event for event in store.events_after(turn['id']) if event['type'] == 'model.requested']) == 1
        assert store.get_immutable_payload(turn['id'], 'answer-gap-binding-gap-drilldown-v1') is None
    measured_median = median(samples)
    print('frozen @4 zero-gap local dispatch milliseconds', samples)
    print('median milliseconds', measured_median)
    assert len(samples) == 5 and len(set(identities)) == 5 and env.model.calls == 5
    assert measured_median <= 250
    assert ACTIVE['retrieve'] == '@3'
