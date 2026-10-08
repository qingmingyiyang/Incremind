"""在原 smallbase 装配中验证每轮复验完整且只检查一次搜索证明。"""
import asyncio
from contextlib import contextmanager
import sys

import pytest

from backend.memory_app.kernel.memory_turn import MemoryTurn
from backend.memory_app.model_config import ModelConfigurationError
from backend.memory_app.recall_preferences import set_preference
from backend.memory_app.v2.privacy import is_private_project, set_private_project
from backend.memory_app.v2.search import search_once, supplement
from backend.memory_app.v2.turn_requests import validate_frozen_inputs
from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from tests.memory_app.v2.test_search_workbench import env, enabled, ask
from tests.memory_app.v2.test_workbench_ask import publish


@contextmanager
def observed_calls(**functions):
    """仅观察原函数的调用事件，不替换 owner 或读取参数与局部变量。"""
    codes = {function.__code__: name for name, function in functions.items()}
    counts = dict.fromkeys(functions, 0)
    previous = sys.getprofile()
    assert previous is None

    def observe(frame, event, argument):
        if event == 'call' and frame.f_code in codes:
            counts[codes[frame.f_code]] += 1

    sys.setprofile(observe)
    try:
        yield counts
    finally:
        sys.setprofile(previous)


def ordinary_plan(env):
    state = env.http.app.state
    # 这个原测试入口没有 legacy container 与 bundled capability 安装器。
    assert getattr(state, 'container', None) is None
    assert getattr(state, 'capability_package_catalog', None) is None
    assert getattr(state, 'capability_package_contributions', None) is None
    insight, _ = publish(env, 'alpha original statement')
    plan = env.domains.query.prepare_ask('alpha', '最近 alpha 展览开放吗')
    assert any(candidate['id'] == insight.id for candidate in plan['chosen'])
    return plan, insight


def search_plan(env):
    enabled(env)
    plan, insight = ordinary_plan(env)
    asyncio.run(supplement(env.domains.query, plan, plan['question'], [plan['question']], key='guard-pass'))
    assert plan['search']['selected'] == 3
    assert len([candidate for candidate in plan['chosen'] if candidate['kind'] == 'search']) == 3
    assert len([call for call in env.wire.calls if 'web_search_options' in call]) == 1
    return plan, insight


@pytest.mark.parametrize('entry', ['validate_ask_plan', '_validate_ask_sources'])
def test_each_complete_or_direct_pass_checks_the_real_search_proof_once(env, entry):
    plan, _ = search_plan(env)
    query = env.domains.query
    with observed_calls(search=plan['search_guard'], plan=query.validate_ask_plan,
                        turn=MemoryTurn.validate, frozen=validate_frozen_inputs) as calls:
        getattr(query, entry)(plan)
    assert calls['search'] == calls['turn'] == calls['frozen'] == 1
    assert calls['plan'] == (2 if entry == 'validate_ask_plan' else 1)


def test_direct_sources_with_empty_chosen_still_reject_changed_search_evidence(env):
    plan, _ = search_plan(env)
    plan['chosen'] = []
    with pytest.raises(RecognitionConflict, match='search_evidence_changed'):
        env.domains.query._validate_ask_sources(plan)


@pytest.mark.parametrize('empty', [False, True])
def test_original_no_search_sources_and_empty_plan_need_no_auxiliary_wire(env, empty):
    plan, _ = ordinary_plan(env)
    if empty:
        plan['chosen'] = []
    env.domains.query._validate_ask_sources(plan)
    env.domains.query.validate_ask_plan(plan)
    assert env.wire.calls == []
    assert env.records.list('v2_memory_turn_keys') == ()


@pytest.mark.parametrize('change', ['off', 'model', 'private'])
def test_each_later_pass_reads_actual_search_authority_again(env, change):
    plan, _ = search_plan(env)
    env.domains.query._validate_ask_sources(plan)
    before = len(env.wire.calls)
    if change == 'private':
        set_private_project(env.records, 'alpha', True, 0)
    else:
        updates = {'enabled': False} if change == 'off' else {'model': 'changed-search-model'}
        env.model.update('search', {**updates, 'expected_revision': 1})
    with pytest.raises((RecognitionError, ModelConfigurationError)):
        env.domains.query._validate_ask_sources(plan)
    assert len(env.wire.calls) == before


@pytest.mark.parametrize('with_search', [False, True])
def test_actual_ordinary_source_withdrawal_is_not_hidden_by_search_deduplication(env, with_search):
    plan, insight = search_plan(env) if with_search else ordinary_plan(env)
    env.domains.query._validate_ask_sources(plan)
    before = len(env.wire.calls)
    source = env.records.read('recognitions', insight.id)
    set_preference(env.records, WorkScope('local-user', 'alpha'), insight.id,
        recognition_revision=insight.revision, preference_revision=0, state='forgotten')
    with pytest.raises(RecognitionError):
        env.domains.query._validate_ask_sources(plan)
    assert env.records.read('recognitions', insight.id) == source
    assert len(env.wire.calls) == before


@pytest.mark.parametrize('change', ['off', 'private'])
def test_search_authority_withdrawn_during_ordinary_read_is_checked_at_pass_end(env, change):
    plan, _ = search_plan(env)
    query = env.domains.query
    code = env.service.get_recognition.__code__
    sources_code = query._validate_ask_sources.__code__
    sources_depth = 0
    changed = False
    previous = sys.getprofile()
    assert previous is None

    def observe(frame, event, argument):
        nonlocal changed, sources_depth
        if frame.f_code is sources_code:
            sources_depth += 1 if event == 'call' else -1 if event == 'return' else 0
        if event == 'return' and frame.f_code is code and sources_depth == 1 and not changed:
            # 只在原普通材料读取完成的事件边界撤销真实资格，不替换读取结果。
            if change == 'off':
                env.model.update('search', {'enabled': False, 'expected_revision': 1})
            else:
                set_private_project(env.records, 'alpha', True, 0)
            changed = True

    before = len(env.wire.calls)
    sys.setprofile(observe)
    try:
        with pytest.raises((RecognitionError, ModelConfigurationError)):
            query._validate_ask_sources(plan)
    finally:
        sys.setprofile(previous)
    assert changed is True and len(env.wire.calls) == before
    if change == 'off':
        assert env.model.public()['search']['enabled'] is False
    else:
        assert is_private_project(env.records, 'alpha') is True


@pytest.mark.parametrize('guard', [None, 'not-a-proof'])
def test_search_candidate_without_a_callable_original_proof_is_rejected(env, guard):
    plan, _ = search_plan(env)
    plan['search_guard'] = guard
    before = len(env.wire.calls)
    with pytest.raises(RecognitionConflict, match='search_proof_missing'):
        env.domains.query._validate_ask_sources(plan)
    assert len(env.wire.calls) == before


def test_saved_query_only_auxiliary_request_still_checks_its_frozen_privacy_revision(env):
    enabled(env)
    result = search_once(env.records, env.model, 'alpha', '最近展览开放吗', key='frozen-guard')
    with observed_calls(turn=MemoryTurn.validate, frozen=validate_frozen_inputs) as calls:
        result['validate_current']()
    assert calls == {'turn': 1, 'frozen': 1}
    # 其它项目改变全局隐私修订，当前项目与搜索配置仍允许；必须由冻结请求拒绝。
    set_private_project(env.records, 'unrelated', True, 0)
    with pytest.raises(RecognitionConflict, match='turn privacy revision conflicted'):
        result['validate_current']()
    assert len(env.wire.calls) == 1


def test_completed_real_smallbase_answer_keeps_empty_chosen_search_validation(env):
    enabled(env)
    response = ask(env, key='completed-search-guard')
    assert response.status_code == 200, response.text
    saved = response.json()
    search_id = saved['turn']['receipt']['ask']['search']['turn_id']
    plans = [plan for plan in env.domains.query.ask_previews.values()
             if plan.get('search', {}).get('turn_id') == search_id]
    assert len(plans) == 1
    plan = plans[0]
    assert plan['state'] == 'completed' and plan['chosen'] == []
    assert len(plan['search_materials']) == 3
    with observed_calls(search=plan['search_guard'], turn=MemoryTurn.validate,
                        frozen=validate_frozen_inputs) as calls:
        env.domains.query._validate_ask_sources(plan)
    assert calls == {'search': 1, 'turn': 1, 'frozen': 1}
    before = len(env.wire.calls)
    env.model.update('search', {'enabled': False, 'expected_revision': 1})
    with pytest.raises(RecognitionConflict, match='search_authority_changed'):
        env.domains.query._validate_ask_sources(plan)
    assert len(env.wire.calls) == before
