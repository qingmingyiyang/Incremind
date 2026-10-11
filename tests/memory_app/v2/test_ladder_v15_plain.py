"""无句末标点的普通认识和连续正文不能仅因标点遗漏而丢失。"""
from copy import deepcopy
import json

import pytest

from backend.memory_app.v2.contextual_chunks import dynamic_contextual_windows
from backend.memory_app.v2.policies import ACTIVE, get, override
from backend.memory_app.answer_proof import REVIEW_KEY
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.search_and_recall.evidence_windows import EvidenceWindow
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture, publish

env = _env_fixture


@pytest.mark.parametrize(('text', 'question'), [
    ('我的学习方法是间隔复习', '我的学习方法是什么？'),
    ('alpha beta gamma', 'alpha beta gamma?'),
    ('我的学习方法是读《如何学习？》并间隔复习', '我的学习方法是什么？'),
])
def test_published_plain_recognition_retains_original_without_terminal_punctuation(env, text, question):
    insight, _ = publish(env, text=text)
    env.model.generation_budget_limits = lambda *, expected_revision, max_tokens: {'window': 16000, 'reserve': max_tokens}
    with override(compose='@28', ladder='@15', retrieve='@5', rerank='@2'):
        plan = env.domains.query.prepare_ask('alpha', question)
        env.domains.query.validate_ask_plan(plan)
    chosen = next(row for row in plan['chosen'] if row['id'] == insight.id)
    assert chosen['layer'] == 'L3' and chosen['entry']['content'] == text
    assert chosen['windows'] == (EvidenceWindow(0, len(text), text),)
    assert chosen['excerpt'].startswith(text)
    assert ACTIVE['ladder'] == '@15'


@pytest.mark.parametrize(('text', 'question'), [
    ('我的学习方法是间隔复习', '我的学习方法是什么？'),
    ('alpha beta gamma', 'alpha beta gamma?'),
])
def test_existing_versions_keep_the_observed_punctuation_behavior(env, text, question):
    publish(env, text=text)
    env.model.generation_budget_limits = lambda *, expected_revision, max_tokens: {'window': 16000, 'reserve': max_tokens}
    query = env.domains.query
    for version, expected in [('@5', 1), ('@14', 0)]:
        with override(compose='@13', ladder=version, retrieve='@3', rerank='@1'):
            plan = query.prepare_ask('alpha', question)
        assert len(plan['chosen']) == expected
    publish(env, text=text + '。')
    with override(compose='@28', ladder='@14', retrieve='@5', rerank='@2'):
        assert len(query.prepare_ask('alpha', question)['chosen']) == 1


@pytest.mark.parametrize(('text', 'question'), [
    ('我的学习方法是间隔复习', '我的学习方法是什么？'),
    ('alpha beta gamma', 'alpha beta gamma?'),
])
def test_plain_source_windows_are_continuous_exact_original_text(text, question):
    content = 'unrelated opening\n\n' + text + '\n\nunrelated ending'
    with override(ladder='@15'):
        selection = dynamic_contextual_windows(content, question)
    assert selection.windows and selection.excerpt.strip() == text
    assert all(content[window.start:window.end] == window.text for window in selection.windows)
    assert all('unrelated' not in window.text for window in selection.windows)


@pytest.mark.parametrize('text', ['我的学习方法是什么？', '我的学习方法是什么',
    'What is alpha beta gamma?', 'What is alpha beta gamma'])
def test_pure_questions_have_no_new_plain_statement_gain(text):
    policy = get('ladder', version='@15')
    demand = policy.demand('alpha beta gamma 学习方法')
    row = {'excerpt': text, 'layer': 'L3', 'kind': 'recognition', 'score': 5}
    assert policy.marginal(row, [], demand) == 0
    assert policy.post_fit_gain(row, [], demand) is False


@pytest.mark.parametrize('row', [
    {'excerpt': '[D1:1] Bo: My alpha beta gamma is useful'},
    {'excerpt': '[D1:1] Ari: What is alpha beta gamma?\n[D1:2] Bo: My alpha beta gamma is useful'},
    {'excerpt': 'Ari alpha beta gamma is useful', 'entry': {'metadata': {'speaker': 'Bo'}}},
    {'excerpt': 'Ari alpha beta gamma is useful', 'entry': {'metadata': {'dialogue': [{'speaker': 'Bo'}]}}},
])
def test_dialogue_and_speaker_metadata_cannot_borrow_plain_statement_gain(row):
    policy = get('ladder', version='@15')
    demand = policy.demand('What is Ari alpha beta gamma?')
    candidate = {'layer': 'L0', 'kind': 'source', 'score': 5, **row}
    assert policy.marginal(candidate, [], demand) == 0
    assert policy.relational_gain(candidate, [], demand) is False


def test_plain_gain_retains_novelty_and_does_not_bypass_discovery_qualification():
    policy = get('ladder', version='@15')
    demand = policy.demand('alpha beta gamma')
    row = {'excerpt': 'alpha beta gamma', 'layer': 'L0', 'kind': 'source', 'score': 5}
    original = deepcopy(row)
    assert policy.marginal(row, [], demand) > 0
    assert policy.marginal(row, [{**row, 'excerpt': 'alpha beta gamma.'}], demand) == 0
    assert policy.marginal({**row, '_discovered_chain': ('untrusted', 0)}, [], demand) == 0
    assert policy.marginal({**row, 'excerpt': 'alpha beta gamma\n…\nnew text'}, [], demand) == 0
    assert row == original


@pytest.mark.parametrize(('text', 'question'), [
    ('我的学习方法是间隔复习', '我的学习方法是什么？'),
    ('alpha beta gamma', 'alpha beta gamma?'),
])
@pytest.mark.parametrize(('window', 'local'), [(200000, False), (16000, False), (16000, True)])
def test_default_plain_recognition_publishes_and_replays_without_new_calls(env, text, question, window, local):
    insight, _ = publish(env, text=text)
    outputs = []

    def complete(messages, *, max_tokens, validate_current=None):
        # 只隔离模型运输，召回、预算、HTTP、Kernel、证明和持久化均运行真实代码。
        validate_current()
        env.model.calls += 1
        payload = json.loads(messages[-1]['content'].split('资料：\n', 1)[1].rsplit('\n\n问题：', 1)[0])
        unit = next(row for row in payload['evidence_catalog']['units'] if text in row['text'])
        fact = {'kind': 'fact', 'text': text, 'date_refs': [],
            'evidence': [{'unit_id': unit['id'], 'quote': text, 'role': 'support'}]}
        if 'untrusted_first_fact_plan' not in payload:
            value = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Only the requested method.'}}
        else:
            assert payload['first_fact_diagnostics'] == []
            fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'adopt', 'reason': 'Retained the original statement.'}]
            value = {'statements': [fact], 'unused': {'scope': 'all_unreferenced', 'reason': 'Only requested evidence.'},
                'uncertain': [], 'excluded_facts': []}
        outputs.append(max_tokens)
        return json.dumps(value), {'usage': {'total_tokens': 11}}

    env.model.complete = complete
    env.model.generation_budget_limits = lambda *, expected_revision, max_tokens: {'window': window, 'reserve': max_tokens}
    if local:
        public = env.model.public
        env.model.public = lambda: {**public(), 'generation': {**public()['generation'],
            'base_url': 'http://127.0.0.1:8001/local-model/v1', 'allow_remote': False}}
    assert env.model.public().get('rerank', {}).get('configured') is not True
    body = {'project_id': 'alpha', 'text': question}
    headers = {'Idempotency-Key': 'ladder15-plain-recognition'}
    response = env.http.post('/api/v2/workbench/turns', headers=headers, json=body)
    assert response.status_code == 200, response.text
    calls_after_publication = env.model.calls
    replay = env.http.post('/api/v2/workbench/turns', headers=headers, json=body)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert env.model.calls == calls_after_publication
    result = response.json()
    receipt = result['turn']['receipt']['ask']
    assert text in receipt['answer'] and receipt['no_match'] is False
    assert [row['id'] for row in receipt['citations']] == [insight.id]
    # 公开认识摘录另带出处说明；原始窗口与最终支持引文保持逐字原文。
    assert all(row['quote'].startswith(text) for row in receipt['citations'])
    assert all(row['locator']['windows'] == [{'start': 0, 'end': len(text)}] for row in receipt['citations'])
    assert len(outputs) == 2 and all(0 < limit <= (512 if local else 7000) for limit in outputs)
    store = SQLiteAITurnStore(env.root / '.rebuild-data' / 'ai-turns.sqlite3')
    request = store.get_request(result['turn']['id'])
    assert {name: request['policy_versions'][name] for name in ('compose', 'ladder', 'retrieve', 'rerank')} == {
        'compose': '@30', 'ladder': '@15', 'retrieve': '@5', 'rerank': '@2'}
    final = store.get_immutable_payload(result['turn']['id'], 'answer-model-result-' + REVIEW_KEY)[1]
    assert final['output']['statements'][0]['evidence'][0]['quote'] == text
    assert len([event for event in store.events_after(result['turn']['id']) if event['type'] == 'model.completed'
        and event['data']['model_call_purpose'] == 'primary']) == 2
