"""默认搜索版本经真实 HTTP 与冻结内核请求生效，模型用途仍默认关闭。"""
from copy import deepcopy

import pytest

from backend.memory_app.v2.policies import ACTIVE, get
from backend.memory_app.v2.policies.pipelines import versions_for_turn
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.memory_turn import MemoryTurn
from tests.memory_app.v2.test_search_workbench import env, ask, enabled
from tests.memory_app.v2.test_workbench_ask import publish


OLD_ACTIVE = {
    'image_read':'@1', 'organize':'@1', 'place':'@1', 'extract':'@3', 'route':'@1',
    'scope':'@2', 'retrieve':'@3', 'rank':'@1', 'strength':'@1', 'forget':'@1',
    'enough':'@1', 'compose':'@3', 'trigger':'@2', 'consolidate':'@2', 'handoff':'@1',
    'reask':'@1', 'outcome_correction':'@1', 'review':'@1', 'elsewhere':'@1', 'gap':'@1',
}


def test_default_search_keeps_every_original_active_value_and_model_purpose_off(env):
    assert ACTIVE == {**OLD_ACTIVE, 'rank':'@2', 'search':'@1'}
    assert get('search') is get('search', version='@1')
    setting = env.http.get('/api/v2/settings').json()['model']['search']
    assert setting['enabled'] is False and setting['allow_remote'] is False
    assert setting['configured'] is False and setting['revision'] == 0
    assert env.wire.calls == []


def test_default_off_ask_freezes_search_without_dispatching_an_auxiliary_turn(env):
    response = ask(env, key='default-search-off')
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['turn']['receipt']['ask']['no_match'] is True
    request = env.http.app.state.ai_turn_store.get_request(saved['turn']['id'])
    assert request['policy_versions']['search'] == '@1'
    assert versions_for_turn('workbench.route')['search'] == '@1'
    assert len(env.wire.calls) == 1
    assert '问法' in env.wire.calls[0]['messages'][0]['content']
    assert not any('web_search_options' in call for call in env.wire.calls)
    assert env.records.list('v2_memory_turn_keys') == ()
    assert env.records.list('workspace_items') == ()


def test_default_enabled_ask_uses_one_search_and_replays_the_frozen_receipt(env):
    enabled(env)
    response = ask(env, key='default-search-enabled')
    assert response.status_code == 200, response.text
    saved = response.json()
    receipt = saved['turn']['receipt']['ask']
    assert receipt['no_match'] is False
    assert receipt['search']['policy_version'] == '@1' and receipt['search']['used'] == 1
    assert receipt['citations'][0]['url'] == 'https://example.test/official/1'
    assert len(receipt['citations']) == len(env.records.list('workspace_items')) == 1
    store = env.http.app.state.ai_turn_store
    assert store.get_request(saved['turn']['id'])['policy_versions']['search'] == '@1'
    aux = MemoryTurn.store_for(env.records).get_request(receipt['search']['turn_id'])
    assert aux['desired_outcome'] == 'web.search' and aux['policy_versions'] == {'search':'@1'}
    assert aux['input']['text'] == '最近春季展览开放吗'
    assert len([call for call in env.wire.calls if 'web_search_options' in call]) == 1
    before_calls, before_originals = len(env.wire.calls), deepcopy(env.records.list('workspace_items'))
    assert ask(env, key='default-search-enabled').json() == saved
    history = env.http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=alpha")
    assert history.json()['turns'][0] == saved['turn']
    assert len(env.wire.calls) == before_calls
    assert env.records.list('workspace_items') == before_originals


@pytest.mark.parametrize('boundary', ['private', 'enough'])
def test_default_enabled_search_keeps_original_zero_wire_boundaries(env, boundary):
    enabled(env)
    if boundary == 'private':
        set_private_project(env.records, 'alpha', True, 0)
        response = ask(env)
        assert response.status_code == 409, response.text
        assert env.wire.calls == []
    else:
        publish(env, text='最近 春季 展览 开放 当前 展览 费用120元')
        env.wire.numbers = [1]
        response = ask(env, '最近 春季 展览 开放')
        assert response.status_code == 200, response.text
        assert response.json()['turn']['receipt']['ask']['trace'][-1]['stopped'] is True
    assert not any('web_search_options' in call for call in env.wire.calls)
    assert env.records.list('v2_memory_turn_keys') == ()


@pytest.mark.parametrize('kind', ['project.answer', 'workbench.route'])
@pytest.mark.parametrize('damage', ['missing', 'unknown'])
def test_default_search_configuration_errors_cannot_silently_remove_the_frozen_version(monkeypatch, kind, damage):
    if damage == 'missing':
        monkeypatch.delitem(ACTIVE, 'search', raising=False)
    else:
        monkeypatch.setitem(ACTIVE, 'search', '@99999')
    with pytest.raises(ValueError, match='unknown_policy_(interface|version)'):
        versions_for_turn(kind)
