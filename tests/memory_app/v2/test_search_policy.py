import json

import pytest

from backend.memory_app.v2.policies import ACTIVE, get, override
from backend.memory_app.v2.policies.types import EnoughInput


def insufficient():
    return EnoughInput('', 0, False, ())


def test_search_results_keep_full_safe_evidence_and_remove_credentials_before_freezing():
    policy = get('search', version='@1')
    key = 'synthetic-private-value'
    rows = [{'url':'https://example.test/price', 'title':'官网', 'text':'费用120元 ' + key},
        {'url':'javascript:alert(1)', 'text':'危险网址'},
        {'url':'https://user:password@example.test', 'text':'用户凭据'},
        {'url':'https://example.test:bad/price', 'text':'非法端口'},
        {'url':'https://example.test/price', 'text':'重复'},
        {'url':'https://example.test/huge', 'text':'过量' * 1201}]
    output = policy.results(rows, credentials=(key,))
    assert output == [{'url':'https://example.test/price', 'title':'来自搜索 官网',
                       'text':'费用120元 [REDACTED_SECRET]'}]
    assert key not in str(output) and key in str(rows)


def test_search_candidate_uses_original_prompt_and_evidence_budgets():
    from backend.memory_app.v2.budget import input_tokens, evidence_tokens, trim_candidate
    policy = get('search', version='@1')
    candidate = {'id':'search-original', 'kind':'search', 'layer':'L0', 'href':'https://example.test',
                 'title':'来自搜索', 'excerpt':'当前开放，费用120元。', 'windows':()}
    with override(search='@1'):
        fitted = policy.fit_evidence(candidate, [], question='最近开放吗', history='', budget=4000, overhead=200,
                            trim=trim_candidate, estimate=input_tokens, evidence_tokens=evidence_tokens)
        denied = policy.fit_evidence(candidate, [], question='最近开放吗', history='', budget=1, overhead=200,
                            trim=trim_candidate, estimate=input_tokens, evidence_tokens=evidence_tokens)
    assert fitted == candidate and denied is None


@pytest.mark.parametrize('question', [
    '今年的规则是什么', '最近有什么变化', '现在流行什么', '目前开放吗',
    '最新的费用', '现行的规定', '当下的价格', '当前的状态',
])
def test_explicit_time_question_can_search_only_when_original_enough_is_false(question):
    policy = get('search', version='@1')
    assert policy(question, insufficient(), candidates=[]) is True
    sufficient = EnoughInput('真实本地依据', .6, False, ('L3',))
    assert get('enough')(sufficient) is True
    assert policy(question, sufficient, candidates=[{'stale':True}]) is False


@pytest.mark.parametrize('question', ['解释这个词', '概括原文', '怎么操作', '项目整体情况'])
def test_ordinary_insufficient_question_does_not_search(question):
    assert get('search', version='@1')(question, insufficient(), candidates=[]) is False


def test_expired_recalled_evidence_can_trigger_search_without_making_persona_evidence():
    policy = get('search', version='@1')
    assert policy('费用多少', insufficient(), candidates=[{'stale':True}]) is True
    assert policy('费用多少', insufficient(), candidates=[{'stale':'true'}]) is False
    assert policy('费用多少', insufficient(), candidates=[{'stale':True, 'persona':True}]) is True
    persona = {'stale':True, 'persona':True, 'kind':'recognition', 'layer':'L3', 'excerpt':'画像正文'}
    decision = policy.sufficient_input([persona], ['画像正文'], lambda text:[(text, 1)], detail=False)
    assert decision.evidence == '' and decision.coverage == 0 and decision.layers == []


def test_search_uses_the_selected_original_enough_detail_rule():
    policy = get('search', version='@1')
    l3_only = EnoughInput('足够词覆盖但没有原文', 1, True, ('L3',))
    l0 = EnoughInput(l3_only.evidence, 1, True, ('L3', 'L0'))
    assert policy('最新数据出处', l3_only, candidates=[]) is True
    assert policy('最新数据出处', l0, candidates=[]) is False


def test_search_prompt_is_query_only_and_uses_the_original_redaction_contract():
    policy = get('search', version='@1')
    messages = policy.messages('现在开放吗 api_key=synthetic-secret-value')
    assert [message['role'] for message in messages] == ['system', 'user']
    data = json.loads(messages[1]['content'])
    assert data['query'].startswith('现在开放吗')
    assert 'synthetic-secret-value' not in str(messages)
    assert '来源' in messages[0]['content']
    assert policy.parameters() == {'max_results':3, 'timeout_seconds':30, 'search_context_size':'medium'}
    mutated = policy.parameters()
    mutated['max_results'] = 999
    assert policy.parameters()['max_results'] == 3


def test_search_registration_keeps_every_active_value_and_supports_explicit_selection():
    before = dict(ACTIVE)
    assert ACTIVE['search'] == '@1' and ACTIVE['rank'] == '@2'
    assert get('search') is get('search', version='@1')
    with override(search='@1'):
        assert get('search') is get('search', version='@1')
    assert ACTIVE == before
