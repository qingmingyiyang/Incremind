"""Real SSE application, SQLite receipts and wire; only transport is synthetic."""
import json

import pytest
from fastapi.testclient import TestClient

from backend.memory_app.kernel.receipt_projection import kernel_call_groups
from tests.memory_app.test_model_costs import RATES, save_prices
from tests.memory_app.v2.test_workbench_stream import events, native_app
from tests.memory_app.v2.test_workbench_ask import env as workbench_env


@pytest.mark.parametrize('usage,duplicate_final,expected', [
    ({'prompt_tokens': 1000, 'completion_tokens': 600, 'prompt_cache_hit_tokens': 200,
      'prompt_cache_miss_tokens': 800}, False, {'currency': 'CNY', 'amount': '0.00342'}),
    ({'prompt_tokens': 1000, 'completion_tokens': 600, 'prompt_cache_hit_tokens': 200,
      'prompt_cache_miss_tokens': 800}, True, {'currency': 'CNY', 'amount': '0.00342'}),
    ({'prompt_tokens': 1000, 'completion_tokens': 600}, False, None),
    ({'prompt_tokens': 1000, 'completion_tokens': 600, 'prompt_cache_hit_tokens': 200,
      'prompt_cache_miss_tokens': 900}, False, None),
])
def test_sse_cost_preserves_actual_cache_counts_and_replay(workbench_env, usage, duplicate_final, expected):
    env = workbench_env
    app, models, _, _ = native_app(env)
    models.update('generation', {'base_url': 'https://proxy.invalid/v1', 'model': 'writer',
        'expected_revision': 1})
    save_prices(models)
    wires, closed = [], []
    raw = json.dumps({'answer': 'Synthetic streamed answer', 'citations': [1]})
    def completion(**request):
        wires.append(request)
        assert len(env.records.list('v2_model_wire_prices')) == 1
        def stream():
            try:
                yield {'choices': [{'delta': {'content': raw}, 'finish_reason': None}]}
                final = {'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': usage}
                yield final
                if duplicate_final:
                    yield final
            finally:
                closed.append(True)
        return stream()
    models._completion_fn = completion
    body = {'project_id': 'alpha', 'text': 'alpha?'}
    headers = {'Accept': 'text/event-stream', 'Idempotency-Key': 'cost-stream'}
    with TestClient(app) as http:
        response = http.post('/api/v2/workbench/turns', json=body, headers=headers)
        assert response.status_code == 200
        parts = events(response)
        assert parts[0][0] == 'started' and parts[-1][0] == 'done'
        final = parts[-1][1]
        assert ''.join(data['text'] for name, data in parts if name == 'delta') == 'Synthetic streamed answer'
        assert final['turn']['receipt']['ask']['model_cost'] == expected
        assert final['turn']['receipt']['ask']['model_usage'] == {
            'input_tokens': 1000, 'output_tokens': 600, 'total_tokens': 1600}
        save_prices(models, {key: '99' for key in RATES}, revision=1)
        replay = http.post('/api/v2/workbench/turns', json=body, headers={
            **headers, 'Accept': 'application/json'})
        assert replay.json() == final
        saved = http.get(f'/api/v2/workbench/threads/{final["thread_id"]}?project_id=alpha').json()
        assert saved['turns'] == [final['turn']]
    calls = kernel_call_groups(env.root, records=env.records)[0]['calls']
    assert len(calls) == 1 and calls[0]['cost'] == expected
    assert len(wires) == 1 and wires[0]['stream'] is True and closed == [True]
    assert len(env.records.list('v2_model_wire_prices')) == 1
    assert 'test-private-value' not in response.text
