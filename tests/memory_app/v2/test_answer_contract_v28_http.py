"""临时HTTP与真实Kernel/proof接线；仅模型运输返回固定JSON。"""
from copy import deepcopy
import json

import pytest

from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input, select_answer_input
from backend.memory_app.v2.policies import override
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture

env = _env_fixture


@pytest.mark.parametrize(('window', 'ladder'), [(200000, '@14'), (16000, '@14'), (16000, '@5')])
def test_v28_adopts_valid_nondate_refs_without_calendar_authority_and_binds_proof(env, window, ladder):
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
            value = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Only requested painting.'}}
        else:
            assert payload['first_fact_diagnostics'] == []
            assert payload['untrusted_first_fact_plan']['facts'][0]['date_refs'] == fact['date_refs']
            fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'adopt', 'reason': 'Kept the directly stated relative fact.'}]
            value = {'statements': [fact], 'unused': {'scope': 'all_unreferenced', 'reason': 'Only requested evidence.'},
                'uncertain': [], 'excluded_facts': []}
        outputs.append((messages, max_tokens, deepcopy(value)))
        return json.dumps(value), {'usage': {'total_tokens': 11}}

    env.model.complete = complete
    env.model.generation_budget_limits = lambda *, expected_revision, max_tokens: {'window': window, 'reserve': max_tokens}
    with override(compose='@28', ladder=ladder, retrieve='@3' if ladder == '@5' else '@5', rerank='@2'):
        response = env.http.post('/api/v2/workbench/turns', headers={'Idempotency-Key': 'v28-valid-ref'},
            json={'project_id': 'alpha', 'text': 'What did Maya paint last week?'})
    assert response.status_code == 200, response.text
    result = response.json()
    receipt = result['turn']['receipt']['ask']
    assert 'Maya painted her self-portrait last week.' in receipt['answer']
    assert '2023-08-14' not in receipt['answer']
    assert receipt['citations'] and len(outputs) == 2
    assert env.model.calls <= 4
    assert all(0 < maximum <= 7000 for _, maximum, _ in outputs)
    store = SQLiteAITurnStore(env.root / '.rebuild-data' / 'ai-turns.sqlite3')
    identity = result['turn']['id']
    request = store.get_request(identity)
    assert request['policy_versions']['compose'] == '@28'
    primary = [e for e in store.events_after(identity) if e['type'] == 'model.completed'
        and e['data']['model_call_purpose'] == 'primary']
    assert len(primary) == 2
    review = store.get_immutable_payload(identity, 'answer-model-input-' + REVIEW_KEY)
    assert read_answer_input(store, identity) == review
    canonical = store.get_immutable_payload(identity, 'answer-model-input-answer')[1]
    draft = store.get_immutable_payload(identity, 'answer-model-result-answer')[1]
    final = store.get_immutable_payload(identity, 'answer-model-result-' + REVIEW_KEY)[1]
    assert draft['output']['facts'][0]['date_refs'] == [1]
    assert final['output']['statements'][0]['date_refs'] == [1]
    capsule = store.get_immutable_payload(identity, 'answer-continuation-plan-v1')[1]
    product = store.get_immutable_payload(identity, 'product-answer-result-v2')[1]
    assert select_answer_input(request, product, canonical, draft, review[1], final, capsule) is review[1]
    tampered = deepcopy(final)
    tampered['output']['statements'][0]['date_refs'] = [999]
    assert select_answer_input(request, product, canonical, draft, review[1], tampered, capsule) is None
    wrong_policy = deepcopy(request)
    wrong_policy['policy_versions']['compose'] = '@27'
    assert select_answer_input(wrong_policy, product, canonical, draft, review[1], final, capsule) is None
