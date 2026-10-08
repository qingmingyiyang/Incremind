"""Real SQLite selection controls; supplied queries are transport-independent."""
import asyncio
from copy import deepcopy
from fractions import Fraction
from threading import Event
from unittest.mock import patch

import pytest

from backend.memory_app.v2 import multi_query
from backend.memory_app.v2.budget import evidence_tokens, input_tokens
from backend.memory_app.v2.policies import ACTIVE, get, override
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionError, WorkScope
from tests.memory_app.v2.test_ladder import env as _env, document, recognition
from tests.memory_app.v2.test_situation_methods import allow, method

env = _env


def prepare(env, question, **kwargs):
    with override(retrieve='@4'):
        collected = env.query.collect_candidates('alpha', question,
            **{key: value for key, value in kwargs.items() if key in {'scene', 'situation'}})
        return env.query.prepare_drilldown('alpha', question, collected=collected, **kwargs), collected


def test_real_prefix_keeps_lower_layers_unselected(env):
    document(env, summary='meridian cobalt', body='meridian cobalt payroll guidance')
    document(env, summary='orbit silica', body='orbit silica measurement contracts')
    plan, _ = prepare(env, 'meridian orbit telescope aperture 具体?')
    assert [row['layer'] for row in plan['trace']] == ['L3', 'L2']
    assert {row['layer'] for row in plan['chosen']} == {'L2'}
    assert plan['drilldown_needed'] is True


def test_real_gap_queries_find_third_source_and_deep_original(env):
    first = document(env, summary='meridian cobalt', body='meridian cobalt payroll guidance')
    second = document(env, summary='orbit silica', body='orbit silica measurement contracts')
    body = 'telescope aperture introduction\n\n' + ('ordinary instrument paragraph ' * 80)
    body += '\n\nomega-detail aperture 17.4 confirmed calibration'
    missing = 'original-only-zeta azimuth observer station photometry correction histogram uncertainty tolerance'
    third = document(env, summary='archive inventory', body=body,
                     original=body + '\n原话 ' + missing)
    question = 'meridian orbit telescope aperture 具体?'
    plan, collected = prepare(env, question)
    upper = deepcopy(plan['chosen'])
    upper_trace = deepcopy(plan['trace'])
    result, used = asyncio.run(multi_query.expand_drilldown(env.query, 'alpha', question, plan, collected,
        ['omega-detail aperture 17.4', missing]))
    assert used['used'] is True
    assert result['chosen'][:len(upper)] == upper
    assert result['trace'][:len(upper_trace)] == upper_trace
    assert {first, second, third} <= {row.get('document_id') for row in result['chosen']}
    assert any('omega-detail' in row['excerpt'] for row in result['chosen'])
    assert any(row['layer'] == 'L0' and 'original-only-zeta' in row['excerpt']
               for row in result['chosen'])
    env.query.validate_ask_plan(result)
    assert evidence_tokens(result['chosen']) <= int(plan['budget'] * .8)
    assert input_tokens(result['chosen'], result['question'], reserve_refutes=True,
        history=result['history']) + result['prompt_overhead'] <= plan['budget']


@pytest.mark.parametrize('coverage,needed', [(0, True), (.599999, True), (.6, False), (1, False)])
def test_new_policy_uses_numeric_upper_coverage_and_preserves_active(coverage, needed):
    policy = get('retrieve', version='@4')
    plan = {'trace': [{'layer': 'L2', 'coverage': coverage, 'stopped': False}]}
    assert policy(None, plan, operation='gap_needed') is needed
    assert ACTIVE['retrieve'] == '@3'


def test_gap_prompt_and_strict_output_are_owned_by_one_new_policy():
    policy = get('retrieve', version='@4')
    messages = policy(None, 'original question', ['L3 observed fact', 'L2 actual summary'], operation='gap_messages')
    assert 'original question' in messages[1]['content']
    assert 'L3 observed fact' in messages[1]['content'] and 'L2 actual summary' in messages[1]['content']
    assert '还缺哪几点' in messages[0]['content']
    assert policy(None, 'original question', {'queries': [' first ', 'FIRST', 'original question', 'second'][:3]},
        operation='gap_queries') == ('first',)
    for bad in ({'queries': ['a'] * 4}, {'queries': ['']}, {'queries': [3]},
                {'queries': ['x' * 201]}, {'queries': ['a'], 'answer': 'invented'}, {'queries': 'a'}):
        with pytest.raises(ValueError, match='invalid_gap_queries'):
            policy(None, 'question', bad, operation='gap_queries')


def test_detail_with_sufficient_l2_uses_no_variant_collection(env, monkeypatch):
    doc = document(env, summary='alpha beta gamma 原文', body='alpha beta gamma 原文 original detail')
    plan, collected = prepare(env, 'alpha beta gamma 原文?')
    assert plan['trace'][-1]['layer'] == 'L2'
    assert plan['trace'][-1]['coverage'] == 1
    assert plan['trace'][-1]['stopped'] is False and plan['drilldown_needed'] is False
    calls = []
    original = env.query.collect_lower_candidates
    def observe(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(env.query, 'collect_lower_candidates', observe)
    result, used = asyncio.run(multi_query.expand_drilldown(env.query, 'alpha', plan['question'], plan, collected, ['another']))
    assert calls == [] and used == {'queries': [], 'used': False}
    assert any(row['layer'] == 'L1' and row['document_id'] == doc for row in result['chosen'])


def test_empty_or_invalid_queries_resume_exact_original_plan_and_budget(env):
    document(env, summary='alpha', body='beta unique body', original='gamma unique original')
    history = 'Earlier question and answer are instruction context only.'
    for queries in ([], [''], ['query'] * 4):
        plan, collected = prepare(env, 'alpha beta gamma delta 原文?', history=history)
        with override(**plan['policy_versions']):
            baseline = env.query.prepare_ask('alpha', plan['question'], collected=collected, history=history)
        result, used = asyncio.run(multi_query.expand_drilldown(env.query, 'alpha', plan['question'], plan, collected, queries))
        assert used == {'queries': [], 'used': False}
        for key in ('chosen', 'trace', 'budget', 'prompt_overhead', 'profile', 'history', 'target', 'policy_versions'):
            assert result[key] == baseline[key]


def test_real_lower_collector_never_retrieves_recognitions_and_preserves_scene(env, monkeypatch):
    own = document(env, summary='alpha', body='telescope local detail', scene='local')
    shared = document(env, summary='alpha', body='telescope project detail')
    sibling = document(env, summary='alpha', body='telescope sibling detail', scene='sibling')
    recognition(env, 'telescope recognition observation')
    calls = []
    original = env.service.retrieval_entries
    def observe(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(env.service, 'retrieval_entries', observe)
    with override(retrieve='@4'):
        lower = env.query.collect_lower_candidates('alpha', 'telescope', scene='local')
    assert calls == []
    assert {row['layer'] for row in lower['candidates']} <= {'L1', 'L0'}
    assert {own, shared} <= {row['document_id'] for row in lower['candidates']}
    assert sibling not in {row['document_id'] for row in lower['candidates']}


def test_temporal_lower_collection_retains_original_valid_owner_without_new_l3(env, monkeypatch):
    doc = document(env, summary='meridian', body='chronometer telescope calibration')
    owner = recognition(env, 'meridian timeline observation', doc=doc)
    future_doc = document(env, summary='meridian', body='chronometer telescope future material')
    with patch('backend.recognition.service._now', return_value='2099-06-12T00:00:00+00:00'):
        future = recognition(env, 'meridian future observation', doc=future_doc)
    plan, collected = prepare(env, '目前 meridian orbit telescope aperture 原文?')
    assert owner.id in {row['id'] for row in collected['candidates'] if row['layer'] == 'L3'}
    assert future.id not in {row['id'] for row in collected['candidates'] if row['layer'] == 'L3'}
    assert collected['time_documents'][doc] == [owner.id]
    calls = []
    original = env.service.retrieval_entries
    def observe(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(env.service, 'retrieval_entries', observe)
    kwargs = dict(policy_versions=plan['policy_versions'], time_scope=plan['time_scope'], time_documents=plan['time_documents'])
    kwargs['time_candidates'] = [row for row in collected['candidates'] if row['layer'] == 'L3']
    lower = env.query.collect_lower_candidates('alpha', 'chronometer telescope', **kwargs)
    assert doc in {row['document_id'] for row in lower['candidates']}
    assert future_doc not in {row['document_id'] for row in lower['candidates']}
    assert calls == [] and all(row['layer'] in {'L1', 'L0'} for row in lower['candidates'])
    votes = multi_query.fuse_candidates([lower['candidates']])
    assert {row['layer'] for row in votes} <= {'L1', 'L0'}
    assert [row['score'] for row in votes] == [float(Fraction(1, 61 + rank)) for rank in range(len(votes))]


@pytest.mark.parametrize('basis_kind', ['other_project', 'sibling_scene'])
def test_temporal_basis_cannot_expand_project_or_scene(env, basis_kind):
    own = document(env, summary='meridian', body='chronometer telescope detail', scene='local')
    recognition(env, 'meridian own observation', doc=own)
    if basis_kind == 'other_project':
        recognition(env, 'meridian other observation', project='other')
        with override(retrieve='@4'):
            outside = env.query.collect_candidates('other', 'meridian')['candidates']
    else:
        sibling = document(env, summary='meridian', body='chronometer sibling detail', scene='sibling')
        recognition(env, 'meridian sibling observation', doc=sibling)
        with override(retrieve='@4'):
            outside = [row for row in env.query.collect_candidates('alpha', 'meridian')['candidates']
                       if row.get('scene') == 'sibling']
    plan, _ = prepare(env, '目前 meridian orbit telescope aperture 原文?', scene='local')
    outside = [row for row in outside if row['layer'] == 'L3']
    assert outside
    with pytest.raises(RecognitionError, match='outside_scope'):
        env.query.collect_lower_candidates('alpha', 'chronometer telescope', scene='local',
            policy_versions=plan['policy_versions'], time_scope=plan['time_scope'],
            time_documents=plan['time_documents'], time_candidates=outside)


def test_actual_revision_race_falls_back_after_all_lower_workers_settle(env, monkeypatch):
    document(env, summary='meridian cobalt', body='meridian payroll guidance')
    document(env, summary='orbit silica', body='orbit contracts measurement')
    third = document(env, summary='archive', body='telescope aperture detail')
    question = 'meridian orbit telescope aperture 具体?'
    plan, collected = prepare(env, question)
    with override(**plan['policy_versions']):
        baseline = env.query.prepare_ask('alpha', question, collected=collected)
    changed, settled = Event(), []
    original = env.query.collect_lower_candidates
    def concurrent_write(project, wording, **kwargs):
        if wording == 'aperture':
            assert changed.wait(5)
        result = original(project, wording, **kwargs)
        if wording == 'telescope':
            env.documents.save_user_edit(third, expected_revision=env.documents.read(third)['revision'],
                markdown='# archive\n\n## 摘要\narchive\n\n## 正文\ntelescope aperture current revision')
            changed.set()
        settled.append(wording)
        return result
    monkeypatch.setattr(env.query, 'collect_lower_candidates', concurrent_write)
    result, used = asyncio.run(multi_query.expand_drilldown(env.query, 'alpha', question, plan, collected,
        ['telescope', 'aperture']))
    assert sorted(settled) == ['aperture', 'telescope']
    assert used == {'queries': [], 'used': False}
    assert result['chosen'] == baseline['chosen'] and result['trace'] == baseline['trace']
    assert third not in {row.get('document_id') for row in result['chosen']}
    env.query.validate_ask_plan(result)


def test_resume_preserves_original_method_and_profile_and_history_guards(env):
    allow(env)
    practical = method(env, '先核实对方近期愿望及尺寸再挑选', ['挑礼物时'])
    recognition(env, '我偏好清晰说明引用依据', project='me')
    question = '给小王选生日礼物颜色预算款式渠道？'
    plan, collected = prepare(env, question, history='Original conversation history')
    frozen_methods = deepcopy(collected['method_candidates'])
    profile = deepcopy(plan['profile'])
    checks = []
    def guard():
        checks.append(True)
    plan['history_guard'] = guard
    result, used = asyncio.run(multi_query.expand_drilldown(env.query, 'alpha', question, plan, collected, ['生日礼物颜色']))
    assert used['used'] is True
    assert collected['method_candidates'] == frozen_methods and result['profile'] == profile
    assert result['history'] == plan['history'] and result['history_guard'] is guard and len(checks) >= 2
    assert any(row['id'] == practical.id and row.get('supplemented') for row in result['chosen'])


def test_real_source_revocation_and_revision_change_prevent_resume(env):
    doc = document(env, summary='alpha', body='beta detail')
    plan, _ = prepare(env, 'alpha beta gamma delta?')
    env.documents.save_user_edit(doc, expected_revision=env.documents.read(doc)['revision'],
        markdown='# changed\n\n## 摘要\nalpha\n\n## 正文\nnew beta content')
    with pytest.raises(RecognitionError, match='changed'):
        env.query.resume_drilldown(plan)
    fresh, _ = prepare(env, 'alpha beta gamma delta?')
    root = fresh['chosen'][0]['snapshot']['roots'][0]
    SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'),
        root['type'], root['id'], root['revision'], 0, [])
    with pytest.raises(RecognitionError):
        env.query.resume_drilldown(fresh)


def test_privacy_change_and_profile_manual_forgetting_prevent_resume(env):
    document(env, summary='alpha', body='beta detail')
    persona = recognition(env, '我喜欢逐项核实数字依据', project='me')
    plan, _ = prepare(env, 'alpha beta gamma delta?')
    from backend.memory_app.recall_preferences import set_preference
    set_preference(env.records, WorkScope('local-user', 'me'), persona.id,
        recognition_revision=1, preference_revision=0, state='forgotten')
    with pytest.raises(RecognitionError):
        env.query.resume_drilldown(plan)
    fresh, _ = prepare(env, 'alpha beta gamma delta?')
    set_private_project(env.records, 'alpha', True, 0)
    with pytest.raises(RecognitionError):
        env.query.resume_drilldown(fresh)
