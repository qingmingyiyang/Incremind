"""Joined answers retain citations from the frozen, complete earlier paragraph."""
import json
from datetime import datetime
import pytest

from fastapi.testclient import TestClient

from tests.memory_app.v2.test_partial_answer import env, interrupted_app
from tests.memory_app.v2.test_workbench_stream import events


@pytest.mark.parametrize('marker', ['[1]', '【1】'], ids=['square', 'fullwidth'])
def test_joined_answer_resolves_old_number_and_records_original_source_usage(env, marker):
    app, _, model, calls, closed, complete = interrupted_app(env, continuation=True)
    complete = complete.replace('[1]', marker)
    def initial_provider(**request):
        calls.append(request)
        def stream():
            try:
                raw = '{"answer":' + json.dumps(complete + '尚未完成的一段', ensure_ascii=False)[:-1]
                yield {'choices': [{'delta': {'content': raw}, 'finish_reason': None}],
                       'usage': {'prompt_tokens': 9, 'completion_tokens': 4}}
                raise ConnectionError('synthetic closed cited paragraph')
            finally:
                closed.append(True)
        return stream()
    model._completion_fn = initial_provider
    with TestClient(app) as http:
        initial = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'retained-citation-initial'})
        saved = events(initial)[-1][1]['turn']
        identity = saved['id']
        assert saved['receipt']['ask']['partial'] == complete and marker in complete
        descriptor = env.records.read('v2_answer_continuations', identity)
        frozen = app.state.ai_turn_store.get(descriptor.payload['plan_ref'])
        from backend.memory_app.kernel.answer_continuations import decode
        plan = decode(frozen['capsule'])['plan']
        chosen = plan['chosen'][0]
        assert chosen['layer'] == 'L3'
        recognition_id = chosen['entry']['id']
        before_usage = env.records.read('v2_usage_insight', recognition_id)
        from backend.memory_app.v2.insights import resolve_insight
        from backend.recognition import WorkScope
        recognition = resolve_insight(env.records, WorkScope('local-user', 'alpha'), recognition_id)
        def tail(**request):
            calls.append(request)
            assert request.get('stream') is True
            def stream():
                try:
                    output = json.dumps({'answer': '接着写完的无引用后文。', 'citations': []}, ensure_ascii=False)
                    yield {'choices': [{'delta': {'content': output}, 'finish_reason': None}]}
                    yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                           'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}
                finally:
                    closed.append(True)
            return stream()
        model._completion_fn = tail
        response = http.post(f'/api/v2/workbench/turns/{identity}/continue', json={'project_id': 'alpha'},
            headers={'Idempotency-Key': 'retained-citation-continue'})
        assert response.status_code == 200, response.text
        receipt = response.json()['receipt']['ask']
        assert receipt['answer'] == complete + '接着写完的无引用后文。'
        assert len(receipt['citations']) == 1, {'citations': receipt['citations'],
                                             'usage_before': before_usage.payload if before_usage else None,
                                             'usage_after': env.records.read('v2_usage_insight', recognition_id).payload}
        citation = receipt['citations'][0]
        assert citation['n'] == 1 and citation['id'] == recognition_id
        assert citation['locator'] == {
            'coordinate_space': chosen.get('coordinate_space', 'workspace_query_content_v1'),
            'windows': [{'start': value.start, 'end': value.end} for value in chosen['windows']]}
        assert citation['quote'] == chosen['excerpt']
        after_usage = env.records.read('v2_usage_insight', recognition_id)
        from backend.shared.memory_sidecars import decayed_score
        from pytest import approx
        baseline = before_usage.payload if before_usage else {
            'project_id': 'alpha', 'score': 1.0, 'count': 1,
            'updated_at': recognition.payload['created_at']}
        assert after_usage.payload['score'] == approx(
            decayed_score(baseline, datetime.fromisoformat(after_usage.payload['updated_at'])) + 1.0)
        assert after_usage.payload['count'] == baseline['count'] + 1
        rows = app.state.ai_turn_store.events_after(identity)
        attempts = [app.state.ai_turn_store.get(row['data']['receipt_ref'])
                    for row in rows if row['type'] == 'model.attempt.terminal']
        assert len(attempts) == 2 and len({item['model_request_id'] for item in attempts}) == 2
        assert attempts[0]['status'] == 'failed_transport' and attempts[1]['status'] == 'succeeded'
        assert app.state.ai_runtime._effect_runner.log.get(attempts[0]['attempt_id']).state.value == 'UNKNOWN'
        assert receipt['model_usage'] == {'input_tokens': 16, 'output_tokens': 7, 'total_tokens': 23}
    assert len(calls) == 2 and closed == [True, True]


@pytest.mark.parametrize('marker', ['[99]', '【99】'], ids=['square', 'fullwidth'])
def test_old_partial_out_of_range_number_rejects_join_without_result_usage_or_history(env, marker):
    from backend.memory_app.v2.followup import read_history
    app, domains, model, calls, closed, _ = interrupted_app(env, continuation=True)
    def provider(**request):
        calls.append(request)
        def stream():
            try:
                if len(calls) == 1:
                    raw = '{"answer":' + json.dumps('旧段非法引用' + marker + '。\n\n半截', ensure_ascii=False)[:-1]
                    yield {'choices': [{'delta': {'content': raw},
                                        'finish_reason': None}], 'usage': {'prompt_tokens': 9, 'completion_tokens': 4}}
                    raise ConnectionError('synthetic closed old paragraph')
                yield {'choices': [{'delta': {'content': '{"answer":"完整后文。","citations":[]}'},
                                    'finish_reason': None}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}
            finally:
                closed.append(True)
        return stream()
    model._completion_fn = provider
    with TestClient(app) as http:
        initial = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'invalid-retained-initial'})
        saved = events(initial)[-1][1]
        identity = saved['turn']['id']
        before_turn = env.records.read('v2_turns', identity)
        assert before_turn.payload['receipt']['ask']['partial'] == '旧段非法引用' + marker + '。\n\n'
        before_usage = {name: env.records.list(name) for name in ('v2_usage_insight', 'v2_usage_document')}
        result = http.post(f'/api/v2/workbench/turns/{identity}/continue', json={'project_id': 'alpha'},
            headers={'Idempotency-Key': 'invalid-retained-continue'})
        assert result.status_code == 502, result.text
        assert app.state.ai_runtime.receipt_for(identity).status == 'failed'
        assert app.state.ai_turn_store.get_immutable_payload(identity, 'product-answer-result-v2') is None
        assert env.records.read('v2_turns', identity) == before_turn
        assert {name: env.records.list(name) for name in before_usage} == before_usage
        assert read_history(env.records, 'alpha', saved['thread_id'], '下一问', query=domains.query)['turns'] == []
    assert len(calls) == 2 and closed == [True, True]
