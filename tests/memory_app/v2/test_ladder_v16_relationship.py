"""状态线索改善发现覆盖，原文与独立探索资格保持。"""
from copy import deepcopy

import pytest

from backend.memory_app.v2.contextual_chunks import dynamic_contextual_windows
from backend.memory_app.v2.ladder import plan_ladder
from backend.memory_app.v2.policies import ACTIVE, get, override
from backend.memory_app.v2.privacy import set_private_project
from core.search_and_recall.evidence_windows import EvidenceWindow
from tests.memory_app.v2.test_ladder import env as env


def candidate(text, *, identifier='original-one', score=1):
    return {'id': identifier, 'kind': 'source', 'layer': 'L0', 'score': score,
        'excerpt': text, 'windows': (EvidenceWindow(0, len(text), text),),
        'coordinate_space': 'source_content_v1', 'entry': {'content': text}}


@pytest.mark.parametrize('version, expected', [('@15', False), ('@16', True)])
def test_relationship_status_finds_future_single_parent_clue_without_vectors(version, expected):
    # 独立人名与原始失败相同的表述结构；计划不改写成当前已婚育状态。
    content = ("[R1:1] Bo: Anything you're excited for in the adoption process?\n"
        "[R1:2] Ari: I'm thrilled to make a family for kids who need one. "
        "It'll be tough as a single parent, but I'm up for the challenge!\n"
        "[R1:3] Bo: You'll be an awesome mom!\n")
    question = "What is Ari's relationship status?"
    with override(ladder=version):
        windows = dynamic_contextual_windows(content, question)
        result = plan_ladder([candidate(content)], question, token_budget=16000)
    assert bool(windows.windows) is expected
    assert bool(result['chosen']) is expected
    if expected:
        assert "It'll be tough as a single parent" in windows.excerpt
        assert all(content[w.start:w.end] == w.text for w in windows.windows)
        chosen = result['chosen'][0]
        assert chosen['entry']['content'] == content
        assert all(content[w.start:w.end] == w.text for w in chosen['windows'])


@pytest.mark.parametrize('body', [
    "I'm single.", "I'm not single.", "I got married last year.",
    "I'm engaged, but the wedding is next year.", "I was divorced five years ago.",
    "My divorce was difficult.", "I went through a tough breakup.",
    "I am dating someone.", "I might stay single.",
    "I'm widowed.", "I plan to be a solo parent.",
])
def test_other_people_states_negation_and_time_are_retained_as_clues(body):
    text = '[X1:1] Nora: ' + body
    with override(ladder='@16'):
        windows = dynamic_contextual_windows(text, "What is Nora's marital status?")
    assert windows.excerpt == text


@pytest.mark.parametrize(('text', 'question'), [
    ('[X1:1] Bo: I am single.', "What is Ari's relationship status?"),
    ('[X1:1] Ari: Are you married?', "What is Ari's relationship status?"),
    ('[X1:1] Ari: I bought a single ticket.', "What is Ari's relationship status?"),
    ('[X1:1] Ari: I am engaged in research.', "What is Ari's relationship status?"),
    ('[X1:1] Ari: I am married to my work.', "What is Ari's relationship status?"),
    ('[X1:1] Ari: My sister is single.', "What is Ari's relationship status?"),
    ('[X1:1] Ari: Bo said "I am single".', "What is Ari's relationship status?"),
    ('[X1:1] Ari: I read about my friend who plans to be a single parent.', "What is Ari's relationship status?"),
    ('[X1:1] Ari: I read a book. [Image caption: I am married.]', "What is Ari's relationship status?"),
    ('[X1:1] Ari: I helped my sister after her divorce.', "What is Ari's relationship status?"),
    ('[X1:1] Ari: I saw a movie about a breakup.', "What is Ari's relationship status?"),
    ('[X1:1] Ari: I helped my brother cope as a single parent.', "What is Ari's relationship status?"),
    ('[X1:1] Ari: I bought dinner. My sister went through a divorce.', "What is Ari's relationship status?"),
    ("[X1:1] Ari: My sister said, 'I am single.'", "What is Ari's relationship status?"),
    ("[X1:1] Ari: I'm single-minded about research.", "What is Ari's relationship status?"),
    ("[X1:1] Ari: I'm single-handedly managing a team.", "What is Ari's relationship status?"),
    ('我的朋友已经结婚。', '婚姻状况如何？'),
    ('我看了一部关于离婚的电影。', '婚姻状况如何？'),
    ('[X1:1] Ari: I am single.', "What is Ari's relationship status with customers at work?"),
    ('[X1:1] Ari: I am single.', "What is Ari's relationship status with friends?"),
    ('[X1:1] Ari: I am single.', "What is Ari's employment status?"),
    ('[X1:1] Ari: I am single.', 'What is Ari reading?'),
])
def test_unrelated_subject_question_and_non_romantic_attributes_have_no_new_gain(text, question):
    policy = get('ladder', version='@16')
    old = get('ladder', version='@15')
    row = candidate(text)
    assert policy.marginal(row, [], policy.demand(question)) == old.marginal(row, [], old.demand(question)) == 0
    with override(ladder='@16'):
        assert not dynamic_contextual_windows(text, question).windows


def test_duplicate_and_unqualified_discovery_chain_cannot_borrow_status_clue():
    policy = get('ladder', version='@16')
    row = candidate('[X1:1] Ari: I am single.')
    original = deepcopy(row)
    demand = policy.demand("What is Ari's relationship status?")
    assert policy.marginal(row, [], demand) > 0
    assert policy.marginal(row, [row], demand) == 0
    assert policy.post_fit_gain({**row, '_discovered_chain': ('untrusted', 0)}, [], demand) is False
    assert policy.marginal({**row, 'persona': True}, [], demand) == 0
    assert row == original
    assert ACTIVE['ladder'] == '@15'


@pytest.mark.parametrize('question', ['我的感情状态是什么？', '我的婚姻状况如何？'])
def test_chinese_relationship_question_keeps_original_status_text(question):
    text = '我已经离婚，正在考虑独自养育孩子。'
    with override(ladder='@16'):
        windows = dynamic_contextual_windows(text, question)
    assert windows.excerpt == text


def test_real_collection_and_frozen_plan_keep_project_and_privacy_scope(env):
    text = "[R1:1] Ari: I am planning to be a single parent, but I have not adopted yet."
    env.query.models.generation_budget_limits = lambda *, expected_revision, max_tokens: {'window': 16000, 'reserve': max_tokens}
    for project, identifier in [('alpha', 'allowed-source'), ('other', 'other-source')]:
        env.query.source_store.write('sources', identifier, {'id': identifier, 'project_id': project,
            'title': 'Family plans', 'metadata': {'content_snapshot': text}}, expected_revision=0)
    with override(ladder='@16'):
        plan = env.query.prepare_ask('alpha', "What is Ari's relationship status?")
        env.query.validate_ask_plan(plan)
        assert [row['id'] for row in plan['chosen']] == ['allowed-source']
        assert plan['chosen'][0]['excerpt'] == text
        set_private_project(env.records, 'alpha', True, expected_revision=0)
        assert env.query.prepare_ask('alpha', "What is Ari's relationship status?")['chosen'] == []


def test_status_clue_cannot_override_real_token_budget_or_admission():
    row = candidate("[R1:1] Ari: I am planning to be a single parent, but I have not adopted yet.")
    with override(ladder='@16'):
        assert plan_ladder([row], "What is Ari's relationship status?", token_budget=0)['chosen'] == []
        assert plan_ladder([row], "What is Ari's relationship status?", token_budget=16000,
            admission=lambda rows: not rows)['chosen'] == []


@pytest.mark.parametrize('text', ['我的朋友已经结婚。', '我看了一部关于离婚的电影。'])
def test_existing_chinese_lexical_noise_does_not_become_new_status_qualification(text):
    from backend.memory_app.v2.policies.ladder import relationship_clues_v16
    policy = get('ladder', version='@16')
    old = get('ladder', version='@15')
    row = candidate(text)
    question = '我的婚姻状况如何？'
    assert relationship_clues_v16(row, policy.demand(question)) == set()
    assert policy.marginal(row, [], policy.demand(question)) == old.marginal(row, [], old.demand(question))


@pytest.mark.parametrize(('beneficiary', 'expected'), [
    ('my sister', False), ('her', False), ('Bo', False), ('me', True), ('us', True),
])
def test_parenting_continuation_keeps_explicit_beneficiary_with_original_owner(beneficiary, expected):
    text = ('[R1:1] Ari: I am excited to adopt children. '
        f'It will be difficult for {beneficiary} as a single parent.')
    row = candidate(text)
    question = "What is Ari's relationship status?"
    policy = get('ladder', version='@16')
    assert bool(policy.marginal(row, [], policy.demand(question))) is expected
    with override(ladder='@16'):
        windows = dynamic_contextual_windows(text, question)
    assert bool(windows.windows) is expected
    if expected:
        assert windows.excerpt == text


def test_chinese_not_yet_married_preserves_full_negation():
    text = '我现在还没有结婚。'
    with override(ladder='@16'):
        windows = dynamic_contextual_windows(text, '感情状态是什么？')
    assert windows.excerpt == text
