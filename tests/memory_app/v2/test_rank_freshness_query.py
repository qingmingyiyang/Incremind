"""沿真实查询、书架和问答冻结路径验证时效，不替换领域服务。"""
from copy import deepcopy

import pytest

from backend.memory_app.v2.policies import override
from backend.recognition import WorkScope
from tests.memory_app.v2.test_bookshelf import forgotten
from tests.memory_app.v2.test_workbench_ask import env, ask


def publish_period(env, conditions, *, project='alpha', content='alpha beta gamma price is 80'):
    scope = WorkScope('local-user', project)
    experience = env.service.stage_experience(scope=scope, content='alpha beta gamma original evidence')
    candidate = env.service.propose(scope=scope, content=content,
        conditions=conditions, source_experience_ids=[experience])
    return env.service.publish(scope=scope, candidate_id=candidate.id,
        expected_revision=1, reviewer='local-user')


@pytest.mark.parametrize('question,methods', [('alpha beta gamma', False),
    ('alpha beta gamma 现在', False), ('周末 alpha beta gamma', True)])
def test_real_query_demotes_expired_insight_in_original_score_paths(env, monkeypatch, question, methods):
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2028-02-01T00:00:00+08:00')
    insight = publish_period(env, ['2025年', '周末'])
    original = env.records.read('recognitions', insight.id)
    with override(rank='@1'):
        old = env.domains.query.collect_candidates('alpha', question)
    with override(rank='@2'):
        new = env.domains.query.collect_candidates('alpha', question)
    key = 'method_candidates' if methods else 'candidates'
    before = next(c for c in old[key] if c['id'] == insight.id)
    after = next(c for c in new[key] if c['id'] == insight.id)
    assert after['score'] == before['score'] * .5
    assert after['stale'] is True and '过时' in after['title']
    assert after['excerpt'] == before['excerpt']
    assert after['windows'] == before['windows']
    assert new['policy_versions']['rank'] == '@2'
    assert env.records.read('recognitions', insight.id) == original
    assert env.model.calls == 0


def test_actual_ask_preserves_stale_citation_and_original_evidence_on_history_read(env, monkeypatch):
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2028-02-01T00:00:00+08:00')
    insight = publish_period(env, ['2025年'])
    original = env.records.read('recognitions', insight.id)
    with override(rank='@2'):
        response = ask(env)
    assert response.status_code == 200, response.text
    saved = response.json()
    receipt = saved['turn']['receipt']['ask']
    citation = next(c for c in receipt['citations'] if c['id'] == insight.id)
    assert citation['stale'] is True
    assert '过时' in env.model.messages[0]['content']
    assert '过时' in env.model.messages[-1]['content']
    assert citation['locator']['windows'] == [{'start': 0, 'end': len(insight.content)}]
    assert env.records.read('recognitions', insight.id) == original
    model_calls = env.model.calls
    frozen = deepcopy(receipt)
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2024-02-01T00:00:00+08:00')
    with override(rank='@1'):
        history = env.http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=alpha")
    assert history.status_code == 200, history.text
    assert history.json()['turns'][0]['receipt']['ask'] == frozen
    assert env.model.calls == model_calls


def test_automatic_bookshelf_restoration_keeps_expired_statement_available_and_marked(env, monkeypatch):
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2028-02-01T00:00:00+08:00')
    insight = publish_period(env, ['2025年'])
    original = env.records.read('recognitions', insight.id)
    forgotten(env, insight)
    with override(rank='@2'):
        response = ask(env)
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    citation = next(c for c in receipt['citations'] if c['id'] == insight.id)
    assert citation['bookshelf'] is True and citation['stale'] is True
    assert '已经淡忘' in env.model.messages[0]['content'] and '过时' in env.model.messages[0]['content']
    assert env.records.read('recognitions', insight.id) == original


def test_real_candidates_keep_the_same_discount_after_original_reciprocal_rank_fusion(env, monkeypatch):
    from backend.memory_app.v2.multi_query import fuse_candidates
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2028-02-01T00:00:00+08:00')
    expired = publish_period(env, ['2025年'])
    current = publish_period(env, ['2028年'])
    with override(rank='@2'):
        collected = env.domains.query.collect_candidates('alpha', 'alpha beta gamma')
    by_id = {c['id']: c for c in collected['candidates']}
    lists = [[by_id[expired.id], by_id[current.id]], [by_id[current.id], by_id[expired.id]]]
    with override(rank='@1'):
        old = {c['id']: c for c in fuse_candidates(lists)}
    with override(rank='@2'):
        new = {c['id']: c for c in fuse_candidates(lists)}
    assert old[expired.id]['score'] == old[current.id]['score']
    assert new[expired.id]['score'] == old[expired.id]['score'] * .5
    assert new[current.id]['score'] == old[current.id]['score']
    assert new[current.id]['rrf_rank'] < new[expired.id]['rrf_rank']


def test_real_variant_workers_reuse_the_initial_frozen_reference_instead_of_current_clock(env, monkeypatch):
    import asyncio
    from backend.memory_app.v2.multi_query import _collect_variants
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2028-02-01T00:00:00+08:00')
    expired = publish_period(env, ['2025年'])
    with override(rank='@2'):
        collected = env.domains.query.collect_candidates('alpha', 'alpha beta gamma')
        monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2024-02-01T00:00:00+08:00')
        variants = asyncio.run(_collect_variants(env.domains.query, 'alpha', ['alpha beta gamma'],
            rank_reference=collected['rank_reference']))
    assert variants[0]['rank_reference'] == collected['rank_reference']
    assert next(c for c in variants[0]['candidates'] if c['id'] == expired.id)['stale'] is True
    assert env.model.calls == 0


def test_real_preview_uses_saved_rank_version_before_wire_even_when_active_changes(env, monkeypatch):
    import asyncio
    from uuid import NAMESPACE_URL, uuid5
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2028-02-01T00:00:00+08:00')
    publish_period(env, ['2025年'])
    query = env.domains.query
    with override(rank='@2'):
        plan = query.prepare_ask('alpha', 'alpha beta gamma')
        preview = query.store_ask_preview(plan)
    assert plan['policy_versions']['rank'] == '@2'
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2024-02-01T00:00:00+08:00')
    with override(rank='@1'):
        result = asyncio.run(query.execute_ask(preview, 'alpha', 'alpha beta gamma', True))
    assert result['answer'] == 'Synthetic answer'
    assert '过时' in env.model.messages[0]['content']
    store = query.answer_turns.application.state.ai_turn_store
    request = store.get_request('turn-' + uuid5(NAMESPACE_URL, 'workspace-ask:' + preview).hex)
    assert request['policy_versions']['rank'] == '@2'
    assert env.model.calls == 1


def test_actual_expired_profile_keeps_stable_prefix_and_only_marks_frozen_context(env, monkeypatch):
    from backend.memory_app.v2.profile import confirmed_profile
    from backend.shared.llm.message_metadata import _estimate_input_tokens
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2028-02-01T00:00:00+08:00')
    expired = publish_period(env, ['2025年'], project='me', content='I prefer tea while reading')
    current = publish_period(env, ['2028年'], project='me', content='I use notebooks to organize plans')
    originals = {identity: env.records.read('recognitions', identity) for identity in (expired.id, current.id)}
    with override(rank='@1'):
        before = confirmed_profile(env.records, env.service)
        legacy_plan = env.domains.query.prepare_ask('alpha', 'unrelated synthetic question?')
    cache = env.records.list('v2_profile_blocks')
    with override(rank='@2'):
        after = confirmed_profile(env.records, env.service)
        plan = env.domains.query.prepare_ask('alpha', 'unrelated synthetic question?')
        response = ask(env, text='unrelated synthetic question?')
    assert after['text'] == before['text']
    assert after['tokens'] == before['tokens'] <= 600
    assert [item['id'] for item in after['items']] == [item['id'] for item in before['items']]
    assert after['projection_basis'] == before['projection_basis']
    assert env.records.list('v2_profile_blocks') == cache
    assert 'instruction' not in before and 'instruction' not in after
    assert plan['profile']['text'] == before['text']
    hint = plan['profile']['instruction']
    assert expired.content in hint and current.content not in hint
    assert plan['prompt_overhead'] == legacy_plan['prompt_overhead'] + _estimate_input_tokens(
        [{'role':'system', 'content':hint}])
    assert plan['rank_reference'] == '2028-02-01T00:00:00+08:00'
    assert plan['chosen'] == legacy_plan['chosen'] == []
    assert env.model.calls > 0
    assert response.status_code == 200, response.text
    saved = response.json()
    receipt = saved['turn']['receipt']['ask']
    assert receipt['citations'] == [] and receipt['layers']['persona'] == 2
    by_id = {item['id']: item for item in receipt['context']['entries'] if item.get('persona')}
    assert by_id[expired.id]['stale'] is True and '过时' in by_id[expired.id]['title']
    assert 'stale' not in by_id[current.id]
    assert all('n' not in entry and 'score' not in entry for entry in by_id.values())
    assert env.model.messages[0]['content'] == before['text']
    assert env.model.messages[-2]['content'].endswith(hint)
    parts = {item['key']:item for item in receipt['context']['parts']}
    assert parts['instruction']['tokens'] >= _estimate_input_tokens([{'role':'system', 'content':hint}])
    assert parts['persona']['count'] == 2 and parts['insight']['count'] == 0
    assert {identity: env.records.read('recognitions', identity) for identity in originals} == originals
    calls = env.model.calls
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2024-02-01T00:00:00+08:00')
    with override(rank='@1'):
        history = env.http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=alpha")
    assert history.status_code == 200, history.text
    assert history.json()['turns'][0]['receipt']['ask'] == receipt
    assert env.model.calls == calls


def test_profile_preview_keeps_expiry_and_instruction_from_its_saved_rank_and_clock(env, monkeypatch):
    import asyncio
    from uuid import NAMESPACE_URL, uuid5
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2028-02-01T00:00:00+08:00')
    expired = publish_period(env, ['2025年'], project='me', content='I prefer quiet reading')
    query = env.domains.query
    with override(rank='@2'):
        plan = query.prepare_ask('alpha', 'unrelated synthetic question?')
        preview = query.store_ask_preview(plan)
    frozen_profile = deepcopy(plan['profile'])
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda: '2024-02-01T00:00:00+08:00')
    with override(rank='@1'):
        result = asyncio.run(query.execute_ask(preview, 'alpha', 'unrelated synthetic question?', True))
    assert result['sources'] == [] and result['model_used'] is True
    assert plan['profile'] == frozen_profile
    assert env.model.messages[0]['content'] == frozen_profile['text']
    assert env.model.messages[-2]['content'].endswith(frozen_profile['instruction'])
    entry = next(item for item in result['context']['entries'] if item['id'] == expired.id)
    assert entry['persona'] is True and entry['stale'] is True
    assert 'n' not in entry and 'score' not in entry
    store = query.answer_turns.application.state.ai_turn_store
    request = store.get_request('turn-' + uuid5(NAMESPACE_URL, 'workspace-ask:' + preview).hex)
    assert request['policy_versions']['rank'] == '@2'
    assert env.model.calls == 1
