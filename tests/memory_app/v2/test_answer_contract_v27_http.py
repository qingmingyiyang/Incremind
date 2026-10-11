"""真实临时HTTP/Kernel/冻结证明路径，只有模型运输使用固定输出替身。"""
import json
import pytest

from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input, select_answer_input
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture

env = _env_fixture


@pytest.mark.parametrize(('window', 'local'), [(200000, False), (16000, False), (16000, True)])
def test_default_two_primary_calls_correct_date_ref_and_bind_public_answer(env, window, local):
    env.domains.query.source_store.write('sources', 'dated-painting', {
        'id': 'dated-painting', 'project_id': 'alpha', 'title': 'Session 2023-08-23',
        'metadata': {'content_snapshot': 'Session date: 2023-08-23\n[D1:1] Maya: I painted this self-portrait last week.'},
    }, expected_revision=0)
    outputs = []

    def complete(messages, *, max_tokens, validate_current=None):
        validate_current()
        env.model.calls += 1
        payload = json.loads(messages[-1]['content'].split('资料：\n', 1)[1].rsplit('\n\n问题：', 1)[0])
        ref = payload['date_provenance'][0]
        fact = {'kind': 'fact', 'text': 'Maya painted her self-portrait last week.',
            'evidence': [{'unit_id': ref['unit_id'], 'quote': 'I painted this self-portrait last week.', 'role': 'support'}],
            'date_refs': [ref['date_ref_id']]}
        if 'untrusted_first_fact_plan' not in payload:
            fact['text'] = 'Maya painted this before 23 August 2023.'
            value = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Only the requested painting.'}}
        else:
            diagnostic, = payload['first_fact_diagnostics']
            assert diagnostic['fact_index'] == 1
            assert diagnostic['codes'] == ['grounded_calendar_provenance_invalid']
            assert diagnostic['issues'][0]['field_path'] == 'facts[0].text'
            assert payload['untrusted_first_fact_plan']['facts'][0]['date_refs'] == fact['date_refs']
            assert payload['untrusted_first_fact_plan'] == outputs[0][2]
            fact.update(kind='date', text='Maya painted it in the week before 23 August 2023: 2023-08-14 to 2023-08-20, or 2023-08-16 to 2023-08-22.')
            fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'correct', 'reason': 'Used an actual date statement for the supported full week ranges.'}]
            value = {'statements': [fact], 'unused': {'scope': 'all_unreferenced', 'reason': 'Only requested evidence.'},
                'uncertain': [], 'excluded_facts': []}
        outputs.append((messages, max_tokens, value))
        return json.dumps(value), {'usage': {'total_tokens': 11}}

    env.model.complete = complete
    env.model.generation_budget_limits = lambda *, expected_revision, max_tokens: {'window': window, 'reserve': max_tokens}
    if local:
        public = env.model.public
        env.model.public = lambda: {**public(), 'generation': {**public()['generation'],
            'base_url': 'http://127.0.0.1:8001/local-model/v1', 'allow_remote': False}}
    assert env.model.public().get('rerank', {}).get('configured') is not True
    headers = {'Idempotency-Key': 'v27-date-correct'}
    body = {'project_id': 'alpha', 'text': 'When did Maya paint her self-portrait?'}
    response = env.http.post('/api/v2/workbench/turns', headers=headers, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    receipt = result['turn']['receipt']['ask']
    assert '2023-08-14 to 2023-08-20' in receipt['answer']
    assert receipt['citations'] and len(outputs) == 2
    assert env.model.calls <= 4
    assert all(0 < maximum <= (512 if local else 7000) for _, maximum, _ in outputs)
    store = SQLiteAITurnStore(env.root / '.rebuild-data' / 'ai-turns.sqlite3')
    identity = result['turn']['id']
    request = store.get_request(identity)
    assert {name: request['policy_versions'][name] for name in ('compose', 'ladder', 'retrieve', 'rerank')} == {
        'compose': '@30', 'ladder': '@15', 'retrieve': '@5', 'rerank': '@2'}
    completed = [e for e in store.events_after(identity) if e['type'] == 'model.completed']
    assert [e['data']['model_call_purpose'] for e in completed if e['data']['model_call_purpose'] == 'primary'] == ['primary', 'primary']
    chosen = read_answer_input(store, identity)
    review = store.get_immutable_payload(identity, 'answer-model-input-' + REVIEW_KEY)
    assert chosen == review
    canonical = store.get_immutable_payload(identity, 'answer-model-input-answer')[1]
    draft = store.get_immutable_payload(identity, 'answer-model-result-answer')[1]
    final = store.get_immutable_payload(identity, 'answer-model-result-' + REVIEW_KEY)[1]
    capsule = store.get_immutable_payload(identity, 'answer-continuation-plan-v1')[1]
    product = store.get_immutable_payload(identity, 'product-answer-result-v2')[1]
    assert select_answer_input(request, product, canonical, draft, review[1], final, capsule) is review[1]
    calls = env.model.calls
    def unexpected(*args, **kwargs):
        raise AssertionError('Completed default answer must not call the model again')
    env.model.complete = unexpected
    replay = env.http.post('/api/v2/workbench/turns', headers=headers, json=body)
    assert replay.status_code == 200 and replay.json() == result
    assert env.model.calls == calls and len(outputs) == 2
    final['output']['statements'][0]['date_refs'] = []
    assert select_answer_input(request, product, canonical, draft, review[1], final, capsule) is None
