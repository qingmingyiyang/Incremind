"""固定模型运输下验证32真实HTTP、两主要调用、冻结证明与缺口消费者。"""
from copy import deepcopy
import json

import pytest

from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input, select_answer_input, review_input_tokens
from backend.memory_app.kernel.receipt_projection import kernel_call_groups
from backend.memory_app.v2.gaps import _frozen_coverage
from backend.memory_app.v2.policies import get, override
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture

env = _env_fixture


@pytest.mark.parametrize('mode', ['date_and_unknown', 'caption_and_question'])
def test_32_actual_http_frozen_reconstruction_and_consumer_tamper_guard(env, mode):
    if mode == 'date_and_unknown':
        content = 'Session date: 2023-09-10\n[D1:1] Nora: We celebrated my graduation last night.\n'
        question = 'When was the graduation celebration and what exact graduation date is known?'
    else:
        content = ('[D1:1] Nora: A necklace. [Image caption: a pendant with a star and a leaf] '
            'It reminds me of family support. The star means hope, right? Blue is my favorite color.\n')
        question = 'Which necklace symbols and meanings are mentioned and what color does Nora prefer?'
    env.domains.query.source_store.write('sources', 'revision-details', {'id': 'revision-details',
        'project_id': 'alpha', 'title': 'Session 2023-09-10' if mode == 'date_and_unknown' else 'Nora notes',
        'metadata': {'content_snapshot': content}}, expected_revision=0)
    outputs = []

    def complete(messages, *, max_tokens, validate_current=None):
        validate_current()
        env.model.calls += 1
        data = json.loads(messages[-1]['content'].split('资料：\n', 1)[1].rsplit('\n\n问题：', 1)[0])
        units = data['evidence_catalog']['units']
        if mode == 'date_and_unknown':
            ref = data['date_provenance'][0]
            unit = next(row for row in units if row['id'] == ref['unit_id'])
            fact = {'kind': 'limitation', 'text': 'The graduation date is unknown; its celebration was on 2023-09-09.',
                'evidence': [{'unit_id': unit['id'], 'quote': 'We celebrated my graduation last night.', 'role': 'support'}],
                'date_refs': [ref['date_ref_id']]}
        else:
            unit = next(row for row in units if 'family support' in row['text'])
            fact = {'kind': 'fact', 'text': 'The necklace means family support.',
                'evidence': [{'unit_id': unit['id'], 'quote': 'It reminds me of family support.', 'role': 'support'}], 'date_refs': []}
        if 'first_evidence_plan' not in data:
            value = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Initial question-directed facts.'}}
        else:
            assert 'untrusted_first_fact_plan' not in data
            assert 'text' not in data['first_evidence_plan'][0]
            fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'correct', 'reason': 'Rebuilt the actual requested statements.'}]
            if mode == 'date_and_unknown':
                fact.update(kind='date', text='The graduation celebration was on 2023-09-09.')
                other = deepcopy(fact)
                other.update(kind='limitation', text='The exact graduation date is not stated.', date_refs=[], first_fact_uses=[])
                statements = [fact, other]
            else:
                fact['text'] = 'The necklace reminds Nora of family support.'
                statements = [fact]
                for text, quote, stance in [
                    ('The necklace has a star and a leaf.', '[Image caption: a pendant with a star and a leaf]', 'asserted'),
                    ('Nora asks whether the star means hope.', 'The star means hope, right?', 'question'),
                    ('Blue is Nora\'s favorite color.', 'Blue is my favorite color.', 'asserted'),
                ]:
                    statements.append({'kind': 'fact', 'text': text, 'evidence': [{'unit_id': unit['id'], 'quote': quote,
                        'role': 'support'}], 'date_refs': [], 'first_fact_uses': [], 'stance': stance})
            for statement in statements:
                statement.setdefault('stance', 'unknown' if statement['kind'] == 'limitation' else 'asserted')
                statement['source_clauses'] = []
                for evidence in statement['evidence']:
                    source = next(row for row in units if row['id'] == evidence['unit_id'])
                    statement['source_clauses'].extend({'unit_id': source['id'], 'quote': row['text']} for row in
                        get('compose', version='@32')(None, operation='modal_clauses', text=source['text'])
                        if row['text'] in evidence['quote'])
            value = {'statements': statements, 'unused': {'scope': 'all_unreferenced', 'reason': 'Only requested details.'},
                'uncertain': [], 'excluded_facts': [], 'review_dispositions': []}
            # 合同运输样本显式关联被采用片段，其余机械位置按一条理由合组。
            excluded = []
            for item in data['review_queue']['items']:
                original = next(row for row in units if row['id'] == item['unit_id'])
                target = original['text'][slice(*item['span'])]
                indices = [index for index, statement in enumerate(statements, 1) if any(
                    evidence['unit_id'] == item['unit_id'] and evidence['role'] == 'support'
                    and (item['scope'] == 'unit_review' or target in evidence['quote']) for evidence in statement['evidence'])]
                if indices:
                    value['review_dispositions'].append({'item_ids': [item['item_id']], 'action': 'include',
                        'statement_indices': indices, 'reason': 'Real statement support covers this location.'})
                else:
                    excluded.append(item['item_id'])
            if excluded:
                value['review_dispositions'].append({'item_ids': excluded, 'action': 'exclude',
                    'statement_indices': [], 'reason': 'No additional requested claim at these locations.'})
        outputs.append((messages, max_tokens, deepcopy(value)))
        return json.dumps(value), {'usage': {'total_tokens': 11}}

    env.model.complete = complete
    env.model.generation_budget_limits = lambda *, expected_revision, max_tokens: {'window': 16000, 'reserve': max_tokens}
    with override(compose='@32', ladder='@16', retrieve='@5', rerank='@2'):
        response = env.http.post('/api/v2/workbench/turns', headers={'Idempotency-Key': 'v32-' + mode},
            json={'project_id': 'alpha', 'text': question})
    assert response.status_code == 200, response.text
    assert len(outputs) == 2 and env.model.calls <= 4
    assert all(0 < maximum <= 7000 for _, maximum, _ in outputs)
    from backend.memory_app.v2.answer_evidence import answer_response_model, final_response_model
    for (messages, maximum, _), schema in zip(outputs, (answer_response_model('@32'), final_response_model('@32', question))):
        assert review_input_tokens(messages, schema) + maximum <= 16000
    turn = response.json()['turn']
    identity = turn['id']
    store = SQLiteAITurnStore(env.root / '.rebuild-data' / 'ai-turns.sqlite3')
    request = store.get_request(identity)
    review = store.get_immutable_payload(identity, 'answer-model-input-' + REVIEW_KEY)
    canonical = store.get_immutable_payload(identity, 'answer-model-input-answer')[1]
    draft = store.get_immutable_payload(identity, 'answer-model-result-answer')[1]
    final = store.get_immutable_payload(identity, 'answer-model-result-' + REVIEW_KEY)[1]
    capsule = store.get_immutable_payload(identity, 'answer-continuation-plan-v1')[1]
    product = store.get_immutable_payload(identity, 'product-answer-result-v2')[1]
    assert read_answer_input(store, identity) == review
    assert select_answer_input(request, product, canonical, draft, review[1], final, capsule) is review[1]
    assert final['output'] == outputs[1][2] and draft['output'] == outputs[0][2]
    group = next(row for row in kernel_call_groups(env.root, project='alpha', remote_only=False, records=env.records,
        include_answer_input=True) if row['turn_id'] == identity)
    assert _frozen_coverage(group, turn['receipt']['ask'], question, 'alpha', get('gap')) is not None
    tampered = deepcopy(review[1])
    tampered['messages'][-1]['content'] = tampered['messages'][-1]['content'].replace('Exact catalog navigation only.', 'Guaranteed complete.', 1)
    assert select_answer_input(request, product, canonical, draft, tampered, final, capsule) is None
    corrupted = deepcopy(group)
    corrupted['answer_input'] = tampered
    assert _frozen_coverage(corrupted, turn['receipt']['ask'], question, 'alpha', get('gap')) is None
