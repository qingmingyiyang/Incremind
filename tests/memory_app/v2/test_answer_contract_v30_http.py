"""真实临时HTTP只新增记录锚语境，固定模型运输不绕过最终guard/proof。"""
from copy import deepcopy
import json

import pytest

from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input, select_answer_input
from backend.memory_app.v2.policies import override
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture

env = _env_fixture


@pytest.mark.parametrize('mode', ['context', 'event', 'ambiguous', 'range', 'same_date', 'chinese_day'])
def test_v30_record_anchor_context_is_checked_per_occurrence_through_http(env, mode):
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
        fact = {'kind': 'fact', 'text': 'Maya painted it in the week before 23 August 2023.',
            'evidence': [{'unit_id': ref['unit_id'], 'quote': 'I painted this self-portrait last week.', 'role': 'support'}],
            'date_refs': [ref['date_ref_id']]}
        if 'untrusted_first_fact_plan' not in payload:
            value = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Only requested painting.'}}
        else:
            issue = payload['first_fact_diagnostics'][0]['issues'][0]
            assert issue['actual_kind'] == 'fact' and issue['date_refs_grant_calculation'] is False
            assert payload['untrusted_first_fact_plan'] == outputs[0][2]
            fact['text'] = 'Maya painted it last week relative to the 23 August 2023 conversation, in the week before 23 August 2023.'
            if mode == 'event':
                fact['text'] += ' She painted it on 23 August 2023.'
            elif mode == 'ambiguous':
                fact['text'] += ' She painted it that day.'
            elif mode == 'range':
                fact['text'] += ' The event date was 2023-08-14 to 2023-08-20.'
            elif mode == 'same_date':
                fact['text'] += ' She painted it on the same date.'
            elif mode == 'chinese_day':
                fact['text'] += ' 她在这一天画了这幅画。'
            fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'correct', 'reason': 'Labeled the real record anchor as context.'}]
            value = {'statements': [fact], 'unused': {'scope': 'all_unreferenced', 'reason': 'Only requested evidence.'},
                'uncertain': [], 'excluded_facts': []}
        outputs.append((messages, max_tokens, deepcopy(value)))
        return json.dumps(value), {'usage': {'total_tokens': 11}}

    env.model.complete = complete
    env.model.generation_budget_limits = lambda *, expected_revision, max_tokens: {'window': 16000, 'reserve': max_tokens}
    with override(compose='@30', ladder='@15', retrieve='@5', rerank='@2'):
        response = env.http.post('/api/v2/workbench/turns', headers={'Idempotency-Key': 'v30-context-' + mode},
            json={'project_id': 'alpha', 'text': 'What did Maya paint last week?'})
    assert len(outputs) == 2 and env.model.calls <= 4
    assert all(0 < maximum <= 7000 for _, maximum, _ in outputs)
    if mode != 'context':
        assert response.status_code == 502 and response.json() == {'detail': 'answer_generation_failed'}
        return
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['turn']['receipt']['ask']['answer']
    store = SQLiteAITurnStore(env.root / '.rebuild-data' / 'ai-turns.sqlite3')
    identity = result['turn']['id']
    request = store.get_request(identity)
    assert request['policy_versions']['compose'] == '@30' and request['policy_versions']['ladder'] == '@15'
    assert len([event for event in store.events_after(identity) if event['type'] == 'model.completed'
        and event['data']['model_call_purpose'] == 'primary']) == 2
    review = store.get_immutable_payload(identity, 'answer-model-input-' + REVIEW_KEY)
    assert read_answer_input(store, identity) == review
    canonical = store.get_immutable_payload(identity, 'answer-model-input-answer')[1]
    draft = store.get_immutable_payload(identity, 'answer-model-result-answer')[1]
    final = store.get_immutable_payload(identity, 'answer-model-result-' + REVIEW_KEY)[1]
    assert draft['output'] == outputs[0][2] and final['output'] == outputs[1][2]
    capsule = store.get_immutable_payload(identity, 'answer-continuation-plan-v1')[1]
    product = store.get_immutable_payload(identity, 'product-answer-result-v2')[1]
    assert select_answer_input(request, product, canonical, draft, review[1], final, capsule) is review[1]
    tampered = deepcopy(final)
    tampered['output']['statements'][0]['text'] += ' She painted it on 23 August 2023.'
    assert select_answer_input(request, product, canonical, draft, review[1], tampered, capsule) is None
