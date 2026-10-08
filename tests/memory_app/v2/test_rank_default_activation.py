"""默认时效版本保留原事实，并在真实查询和问答历史中冻结过时标记。"""
from copy import deepcopy

from backend.memory_app.v2.policies import ACTIVE, get, override
from backend.memory_app.v2.policies.types import RankInput
from tests.memory_app.v2.test_search_default_activation import OLD_ACTIVE
from tests.memory_app.v2.test_rank_freshness_query import publish_period
from tests.memory_app.v2.test_workbench_ask import env, ask


def test_default_rank_selects_only_the_new_freshness_version():
    assert ACTIVE == {**OLD_ACTIVE, 'rank':'@2', 'search':'@1'}
    assert get('rank') is get('rank', version='@2')
    unknown = RankInput(3, .7)
    assert get('rank')(unknown) == get('rank', version='@1')(unknown) == (3 + 1) * .7


def test_default_real_query_applies_one_half_without_rewriting_the_recognition(env, monkeypatch):
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda:'2028-02-01T00:00:00+08:00')
    insight = publish_period(env, ['2025年'])
    original = env.records.read('recognitions', insight.id)
    with override(rank='@1'):
        before = env.domains.query.collect_candidates('alpha', 'alpha beta gamma')
    after = env.domains.query.collect_candidates('alpha', 'alpha beta gamma')
    old = next(row for row in before['candidates'] if row['id'] == insight.id)
    new = next(row for row in after['candidates'] if row['id'] == insight.id)
    assert new['score'] == old['score'] * .5
    assert new['stale'] is True and new['title'].startswith('过时')
    assert new['excerpt'] == old['excerpt'] and new['windows'] == old['windows']
    assert after['policy_versions']['rank'] == '@2'
    assert env.records.read('recognitions', insight.id) == original
    assert env.model.calls == 0


def test_default_real_ask_freezes_stale_citation_and_history_does_not_recompute_it(env, monkeypatch):
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda:'2028-02-01T00:00:00+08:00')
    insight = publish_period(env, ['2025年'])
    original = env.records.read('recognitions', insight.id)
    with override(rank='@1'):
        before = env.domains.query.collect_candidates('alpha', 'alpha beta gamma')
    original_excerpt = next(row['excerpt'] for row in before['candidates'] if row['id'] == insight.id)
    response = ask(env)
    assert response.status_code == 200, response.text
    saved = response.json()
    receipt = saved['turn']['receipt']['ask']
    citation = next(row for row in receipt['citations'] if row['id'] == insight.id)
    assert citation['stale'] is True
    assert '过时' in env.model.messages[0]['content'] and '过时' in env.model.messages[-1]['content']
    assert citation['quote'] == original_excerpt
    assert insight.content in citation['quote'] and '2025年' in citation['quote']
    assert citation['locator']['windows'] == [{'start':0, 'end':len(insight.content)}]
    assert env.records.read('recognitions', insight.id) == original
    request = env.http.app.state.ai_turn_store.get_request(saved['turn']['id'])
    assert request['policy_versions']['rank'] == '@2' and request['policy_versions']['search'] == '@1'
    before_calls, frozen = env.model.calls, deepcopy(saved['turn'])
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda:'2024-02-01T00:00:00+08:00')
    with override(rank='@1'):
        history = env.http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=alpha")
    assert history.status_code == 200, history.text
    assert history.json()['turns'][0] == frozen
    assert env.model.calls == before_calls
