"""Real domain recall of applicable methods, without model generation."""
import json
from types import SimpleNamespace

import pytest

from backend.memory_app.v2.budget import evidence_tokens, input_tokens
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.policies import override
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import WorkScope
from tests.memory_app.v2.test_ladder import env as _env

env = _env


@pytest.fixture(autouse=True)
def method_versions():
    with override(retrieve='@2', compose='@2'):
        yield


def method(env, body, conditions, *, project="alpha", scene=None):
    scope = WorkScope("local-user", project)
    experience = env.service.stage_experience(scope=scope, content="Synthetic method source")
    proposal = env.service.propose(scope=scope, content=body, conditions=conditions,
                                  source_experience_ids=[experience])
    published = env.service.publish(scope=scope, candidate_id=proposal.id,
                                    expected_revision=1, reviewer="local-user")
    if scene:
        assign_scene(env.records, "recognition", published.id, project, scene)
    return published


def allow(env, allowed=True):
    env.query.models = SimpleNamespace(public=lambda: {
        "generation": {"base_url": "https://example.invalid/v1", "allow_remote": allowed,
                       "revision": 1, "model": "synthetic"}})


def supplements(plan):
    return [row for row in plan["chosen"] if row.get("supplemented")]


def test_conditions_recall_project_method_without_body_match_and_isolate_sibling(env):
    allow(env)
    project = method(env, "先问清预算和对方近期愿望", ["挑礼物时"])
    sibling = method(env, "只看银质饰品", ["挑礼物时"], scene="小李")
    local = method(env, "对方已有装备先避开", ["挑礼物时"], scene="小王")
    plan = env.query.prepare_ask("alpha", "给小王送什么生日礼物？", scene="小王")
    ids = {row["id"] for row in supplements(plan)}
    assert ids == {project.id, local.id}
    assert sibling.id not in {row["id"] for row in plan["chosen"]}


def test_body_match_alone_does_not_make_a_method_applicable(env):
    allow(env)
    one = method(env, "生日礼物先问清预算", ["写论文时"])
    plan = env.query.prepare_ask("alpha", "生日礼物怎么选？")
    assert one.id not in {row["id"] for row in supplements(plan)}


def test_input_covered_keywords_are_not_supplemented(env):
    allow(env)
    one = method(env, "先问清预算和近期愿望", ["挑礼物时"])
    plan = env.query.prepare_ask("alpha", "生日礼物先问清预算和近期愿望，再选什么？")
    assert one.id not in {row["id"] for row in supplements(plan)}


def test_supplements_have_three_limit_and_share_evidence_budget(env):
    allow(env)
    for body in ("比较实用程度", "先问最近爱好", "留出换货余地", "考虑收纳空间", "核实尺寸颜色"):
        method(env, body, ["挑礼物时"])
    plan = env.query.prepare_ask("alpha", "给小王选生日礼物？")
    assert len(supplements(plan)) == 3
    assert evidence_tokens(plan["chosen"]) <= int(plan["budget"] * .8)
    assert input_tokens(plan["chosen"], plan["question"], reserve_refutes=True) + plan["prompt_overhead"] <= plan["budget"]


def test_private_and_remote_disabled_methods_are_not_supplemented(env):
    allow(env)
    one = method(env, "先问最近爱好", ["挑礼物时"])
    SourceEgressService(env.records).set_policy(WorkScope("local-user", "alpha"),
        "recognition", one.id, 1, 0, [])
    assert supplements(env.query.prepare_ask("alpha", "给小王选生日礼物？")) == []
    method(env, "留出换货余地", ["挑礼物时"])
    allow(env, False)
    assert supplements(env.query.prepare_ask("alpha", "给小王选生日礼物？")) == []
    set_private_project(env.records, "alpha", True, 0)
    assert supplements(env.query.prepare_ask("alpha", "给小王选生日礼物？")) == []


def test_repeated_preparation_preserves_exact_prompt_bytes(env):
    allow(env)
    method(env, "先问最近爱好", ["挑礼物时"])
    from backend.memory_app.v2.budget import ask_instruction, source_texts, user_text
    def frozen():
        plan = env.query.prepare_ask("alpha", "给小王选生日礼物？")
        return json.dumps({"policy_versions": plan["policy_versions"], "messages": [
            {"role": "system", "content": ask_instruction(plan["chosen"])},
            {"role": "user", "content": user_text(source_texts(plan["chosen"]), plan["question"])}]},
            ensure_ascii=False, sort_keys=True).encode()
    assert frozen() == frozen()


def test_original_instruction_and_explicit_situation_survive_condensed_wording(env):
    allow(env)
    one = method(env, '先问最近爱好', ['挑礼物时'])
    condensed = env.query.collect_candidates('alpha', '她喜欢哪款？')
    plan = env.query.prepare_ask('alpha', '给小王送什么生日礼物？',
        retrieval_question='她喜欢哪款？', collected=condensed)
    assert {row['id'] for row in supplements(plan)} == {one.id}
    routed = env.query.prepare_ask('alpha', '帮我想一个方案', situation='挑礼物')
    assert {row['id'] for row in supplements(routed)} == {one.id}


def test_legacy_policy_keeps_original_framing_and_never_adds_methods(env):
    allow(env)
    method(env, '先问最近爱好', ['挑礼物时'])
    from backend.memory_app.v2.budget import ask_instruction, source_texts
    with override(retrieve='@1', compose='@1'):
        plan = env.query.prepare_ask('alpha', '给小王选生日礼物？')
        assert plan['chosen'] == []
        row = {'id': 'legacy', 'title': '标题', 'excerpt': '原始证据', 'supplemented': True}
        assert source_texts([row])[0].encode() == '[1] 标题\n原始证据'.encode()
        assert ask_instruction([row]).encode() == ('只根据用户提供的资料回答。返回 JSON 对象：answer 为简洁中文答案，citations 为实际使用的资料编号整数数组。'
            '没有依据时明确说不知道，不编造来源。不要输出 Markdown 围栏。').encode()


def test_method_materials_freeze_byte_identically_with_same_turn_inputs(env):
    allow(env)
    one = method(env, '留出换货余地', ['挑礼物时'])
    from backend.memory_app.v2.turn_requests import freeze_product_turn
    descriptor = {'type': 'recognition', 'id': one.id, 'revision': 1, 'project_id': 'alpha'}
    def frozen():
        request = freeze_product_turn('project.task', records=env.records, models=env.query.models,
            project_id='alpha', materials=[descriptor], load_text=lambda item: '',
            text='给小王送生日礼物', turn_id='turn-frozen-method', session_id='session-frozen-method',
            operation_id='operation-frozen-method', idempotency_key='key-frozen-method',
            created_at='2026-10-05T00:00:00+00:00')
        return json.dumps(request, sort_keys=True, ensure_ascii=False).encode()
    assert frozen() == frozen()


def test_new_policy_preserves_all_ordinary_prompt_bytes_and_token_budgets(env):
    allow(env)
    method(env, 'alpha beta gamma', [])
    from backend.memory_app.v2.budget import ask_instruction, source_texts, user_text, history_tokens
    def ordinary(version):
        with override(retrieve=version, compose=version):
            plan = env.query.prepare_ask('alpha', 'alpha beta?', history='earlier synthetic question')
            assert not supplements(plan)
            variants = [plan['chosen'], [{**row, 'bookshelf': True} for row in plan['chosen']],
                        [{**row, 'link_kind': 'refutes'} for row in plan['chosen']]]
            framing = [(ask_instruction(rows).encode(), tuple(text.encode() for text in source_texts(rows)),
                user_text(source_texts(rows), plan['question'], plan['history']).encode(),
                evidence_tokens(rows), input_tokens(rows, plan['question'], reserve_refutes=True,
                                                   history=plan['history'])) for rows in variants]
            return plan['chosen'], plan['trace'], plan['budget'], plan['prompt_overhead'], framing, history_tokens(plan['history'])
    assert ordinary('@2') == ordinary('@1')


def test_profile_methods_stay_in_profile_without_duplicate_project_supplements(env):
    allow(env)
    persona = method(env, '先保留一个可验证的小步骤', ['挑礼物时'], project='me')
    local = method(env, '留出换货余地', ['挑礼物时'])
    from backend.memory_app.v2.profile import confirmed_profile
    with override(compose='@1'):
        original_profile = confirmed_profile(env.records, env.service)['text'].encode()
    plan = env.query.prepare_ask('alpha', '给小王选生日礼物？')
    assert {row['id'] for row in supplements(plan)} == {local.id}
    assert {row['id'] for row in plan['profile']['items']} == {persona.id}
    assert '先保留一个可验证的小步骤' in plan['profile']['text']
    assert plan['profile']['text'].encode() == original_profile
