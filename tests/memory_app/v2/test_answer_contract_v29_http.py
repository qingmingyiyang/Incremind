"""临时HTTP与真实Kernel/proof的首稿修复接线；仅模型运输固定JSON。"""
from copy import deepcopy
import json

import pytest

from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input, select_answer_input
from backend.memory_app.v2.policies import override
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture

env = _env_fixture


@pytest.mark.parametrize('mode', ['ref', 'text', 'role', 'quote', 'article'])
def test_v29_real_http_repairs_first_fields_and_freezes_two_call_proof(env, mode):
    _exercise(env, mode, repair=True)


@pytest.mark.parametrize('mode', ['ref', 'text', 'role', 'quote'])
def test_v29_real_http_reason_without_field_repair_still_rejects(env, mode):
    _exercise(env, mode, repair=False)


@pytest.mark.parametrize('mode', ['text', 'article'])
def test_v29_repair_and_bound_calendar_integrate_with_ladder15(env, mode):
    _exercise(env, mode, repair=True, ladder='@15')


def _exercise(env, mode, *, repair, ladder='@14'):
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
            'date_refs': []}
        if mode == 'ref':
            fact['date_refs'] = [999]
        elif mode == 'text':
            fact.update(kind='inference', text='Maya painted this before 23 August 2023.')
        elif mode == 'role':
            fact['kind'] = 'inference'
            fact['evidence'][0]['role'] = 'context'
        elif mode == 'quote':
            fact['evidence'][0]['quote'] = '23 August 2023'
        else:
            fact.update(kind='date', date_refs=[ref['date_ref_id']],
                text='Maya painted it relative to the 23 August 2023 session date: 2023-08-14 to 2023-08-20.')
        if 'untrusted_first_fact_plan' not in payload:
            value = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Only requested painting.'}}
        else:
            assert list(payload)[-2:] == ['date_provenance', 'evidence_catalog']
            assert payload['untrusted_first_fact_plan'] == outputs[0][2]
            if mode == 'article':
                assert payload['first_fact_diagnostics'] == []
            else:
                assert payload['first_fact_diagnostics'][0]['fact_index'] == 1
                assert payload['first_fact_diagnostics'][0]['issues'][0]['field_path'].startswith('facts[0].')
            if repair:
                if mode == 'ref':
                    fact['date_refs'] = []
                elif mode == 'text':
                    fact['text'] = 'Maya painted her self-portrait.'
                elif mode == 'role':
                    fact.update(kind='limitation', text='The exact painting date is not established.')
                elif mode == 'quote':
                    fact['evidence'][0]['quote'] = 'I painted this self-portrait last week.'
            fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'adopt' if mode == 'article' else 'correct',
                'reason': 'Repaired the diagnosed field.'}]
            value = {'statements': [fact], 'unused': {'scope': 'all_unreferenced', 'reason': 'Only requested evidence.'},
                'uncertain': [], 'excluded_facts': []}
        outputs.append((messages, max_tokens, deepcopy(value)))
        return json.dumps(value), {'usage': {'total_tokens': 11}}

    env.model.complete = complete
    env.model.generation_budget_limits = lambda *, expected_revision, max_tokens: {'window': 16000, 'reserve': max_tokens}
    with override(compose='@29', ladder=ladder, retrieve='@5', rerank='@2'):
        response = env.http.post('/api/v2/workbench/turns', headers={'Idempotency-Key': 'v29-fields-' + mode},
            json={'project_id': 'alpha', 'text': 'What did Maya paint last week?'})
    assert len(outputs) == 2 and env.model.calls <= 4
    assert all(0 < maximum <= 7000 for _, maximum, _ in outputs)
    if not repair:
        assert response.status_code == 502
        assert response.json() == {'detail': 'answer_generation_failed'}
        return
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['turn']['receipt']['ask']['answer']
    store = SQLiteAITurnStore(env.root / '.rebuild-data' / 'ai-turns.sqlite3')
    identity = result['turn']['id']
    request = store.get_request(identity)
    assert request['policy_versions']['compose'] == '@29'
    assert request['policy_versions']['ladder'] == ladder
    primary = [event for event in store.events_after(identity) if event['type'] == 'model.completed'
        and event['data']['model_call_purpose'] == 'primary']
    assert len(primary) == 2
    review = store.get_immutable_payload(identity, 'answer-model-input-' + REVIEW_KEY)
    assert read_answer_input(store, identity) == review
    canonical = store.get_immutable_payload(identity, 'answer-model-input-answer')[1]
    draft = store.get_immutable_payload(identity, 'answer-model-result-answer')[1]
    final = store.get_immutable_payload(identity, 'answer-model-result-' + REVIEW_KEY)[1]
    assert draft['output'] == outputs[0][2]
    assert final['output'] == outputs[1][2]
    capsule = store.get_immutable_payload(identity, 'answer-continuation-plan-v1')[1]
    product = store.get_immutable_payload(identity, 'product-answer-result-v2')[1]
    assert select_answer_input(request, product, canonical, draft, review[1], final, capsule) is review[1]
    tampered = deepcopy(final)
    tampered['output']['statements'][0]['evidence'][0]['quote'] = 'fabricated final quote'
    assert select_answer_input(request, product, canonical, draft, review[1], tampered, capsule) is None
