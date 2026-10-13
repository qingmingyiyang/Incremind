"""临时HTTP完整走冻结、纠错、证明和缺口读模型，模型运输使用固定JSON。"""
from copy import deepcopy
import json

import pytest

from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input, select_answer_input
from backend.memory_app.kernel.receipt_projection import kernel_call_groups
from backend.memory_app.v2.gaps import _frozen_coverage
from backend.memory_app.v2.policies import get, override
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture

env = _env_fixture


@pytest.mark.parametrize('mode', ['date_and_unknown', 'caption_and_question'])
def test_review_queue_is_frozen_through_real_http_and_consumers_reject_tampering(env, mode):
    if mode == 'date_and_unknown':
        content = 'Session date: 2023-09-10\n[D1:1] Nora: We celebrated my graduation last night.\n'
        question = "When was Nora's graduation celebration and what is the exact graduation date?"
    else:
        content = ('[D1:1] Nora: A necklace. [Image caption: a pendant with a star and a leaf] '
            'It reminds me of family support. The star means hope, right? Blue is my favorite color.\n')
        question = "What symbols are on Nora's necklace, what do they mean, and what is her favorite color?"
    env.domains.query.source_store.write('sources', 'review-details', {
        'id': 'review-details', 'project_id': 'alpha', 'title': 'Session 2023-09-10' if mode == 'date_and_unknown' else 'Nora notes',
        'metadata': {'content_snapshot': content},
    }, expected_revision=0)
    outputs = []

    def complete(messages, *, max_tokens, validate_current=None):
        validate_current()
        env.model.calls += 1
        payload = json.loads(messages[-1]['content'].split('资料：\n', 1)[1].rsplit('\n\n问题：', 1)[0])
        assert payload['review_queue']['boundary'].startswith('Mechanical locations only;')
        units = payload['evidence_catalog']['units']
        if mode == 'date_and_unknown':
            ref = payload['date_provenance'][0]
            fact = {'kind': 'limitation', 'text': 'The exact graduation date is unknown, but the celebration was on 2023-09-09.',
                'evidence': [{'unit_id': ref['unit_id'], 'quote': 'We celebrated my graduation last night.', 'role': 'support'}],
                'date_refs': [ref['date_ref_id']]}
        else:
            unit = next(unit for unit in units if 'family support' in unit['text'])
            fact = {'kind': 'fact', 'text': 'The necklace reminds Nora of family support.',
                'evidence': [{'unit_id': unit['id'], 'quote': 'It reminds me of family support.', 'role': 'support'}], 'date_refs': []}
        if 'untrusted_first_fact_plan' not in payload:
            value = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Initial requested information.'}}
        else:
            assert payload['untrusted_first_fact_plan'] == outputs[0][2]
            fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'correct', 'reason': 'Retained the supported detail and separated the uncertainty.'}]
            if mode == 'date_and_unknown':
                assert payload['review_queue']['date_classification_repairs'][0]['fact_index'] == 1
                fact.update(kind='date', text="Nora's graduation celebration was on 2023-09-09.")
                companion = deepcopy(fact)
                companion.update(kind='limitation', text='The exact graduation date is not stated.', date_refs=[], first_fact_uses=[])
                statements = [fact, companion]
            else:
                assert payload['review_queue']['detail_spans']
                caption = next(unit for unit in units if 'Image caption' in unit['text'])
                fact['text'] = 'The necklace has a star and a leaf, and reminds Nora of family support.'
                fact['evidence'].append({'unit_id': caption['id'], 'quote': '[Image caption: a pendant with a star and a leaf]', 'role': 'support'})
                companion = {'kind': 'fact', 'text': 'Nora asks whether the star means hope; blue is her favorite color.',
                    'evidence': [{'unit_id': unit['id'], 'quote': 'The star means hope, right? Blue is my favorite color.', 'role': 'support'}],
                    'date_refs': [], 'first_fact_uses': []}
                statements = [fact, companion]
            value = {'statements': statements, 'unused': {'scope': 'all_unreferenced', 'reason': 'Only requested details with their original status.'},
                'uncertain': [], 'excluded_facts': []}
        outputs.append((messages, max_tokens, deepcopy(value)))
        return json.dumps(value), {'usage': {'total_tokens': 11}}

    env.model.complete = complete
    env.model.generation_budget_limits = lambda *, expected_revision, max_tokens: {'window': 16000, 'reserve': max_tokens}
    with override(compose='@31', ladder='@15', retrieve='@5', rerank='@2'):
        response = env.http.post('/api/v2/workbench/turns', headers={'Idempotency-Key': 'v31-' + mode},
            json={'project_id': 'alpha', 'text': question})
    assert response.status_code == 200, response.text
    assert len(outputs) == 2 and env.model.calls <= 4
    assert all(0 < maximum <= 7000 for _, maximum, _ in outputs)
    turn = response.json()['turn']
    identity = turn['id']
    store = SQLiteAITurnStore(env.root / '.rebuild-data' / 'ai-turns.sqlite3')
    request = store.get_request(identity)
    assert request['policy_versions']['compose'] == '@31'
    review = store.get_immutable_payload(identity, 'answer-model-input-' + REVIEW_KEY)
    assert read_answer_input(store, identity) == review
    canonical = store.get_immutable_payload(identity, 'answer-model-input-answer')[1]
    draft = store.get_immutable_payload(identity, 'answer-model-result-answer')[1]
    final = store.get_immutable_payload(identity, 'answer-model-result-' + REVIEW_KEY)[1]
    capsule = store.get_immutable_payload(identity, 'answer-continuation-plan-v1')[1]
    product = store.get_immutable_payload(identity, 'product-answer-result-v2')[1]
    assert draft['output'] == outputs[0][2] and final['output'] == outputs[1][2]
    assert select_answer_input(request, product, canonical, draft, review[1], final, capsule) is review[1]
    tampered = deepcopy(review[1])
    text = tampered['messages'][-1]['content']
    tampered['messages'][-1]['content'] = text.replace('Mechanical locations only;', 'Proven complete;', 1)
    assert select_answer_input(request, product, canonical, draft, tampered, final, capsule) is None
    group = next(group for group in kernel_call_groups(env.root, project='alpha', remote_only=False,
        records=env.records, include_answer_input=True) if group['turn_id'] == identity)
    assert _frozen_coverage(group, turn['receipt']['ask'], question, 'alpha', get('gap')) is not None
    corrupted = deepcopy(group)
    corrupted['answer_input'] = tampered
    assert _frozen_coverage(corrupted, turn['receipt']['ask'], question, 'alpha', get('gap')) is None
