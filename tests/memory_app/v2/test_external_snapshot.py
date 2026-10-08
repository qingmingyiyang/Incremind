"""Static copy uses the original external Turn and qualified local memories."""
from copy import deepcopy

import pytest

from backend.memory_app.v2.policies import ACTIVE
from backend.memory_app.v2.privacy import set_private_project
from core.effect_log import EffectState
from tests.memory_app.v2.test_external_context import env, settings
from tests.memory_app.v2.test_mcp_recall import method


PATH = '/api/v2/settings/external-agent/snapshot'


def snapshot(env, **changes):
    return env.http.post(PATH, json={'client': 'codex', 'project_id': 'alpha', 'budget': 3000} | changes)


def test_snapshot_contains_project_methods_and_profile_and_records_real_delivery(env):
    first, *_ = method(env, content='给朋友挑选礼物先问愿望', conditions=('挑礼物时',))
    second, *_ = method(env, content='写论文先列论点和依据', conditions=('写论文时',))
    fact, *_ = method(env, content='一个普通事实', conditions=())
    foreign, *_ = method(env, project='beta', content='其它项目不应交出', conditions=('写论文时',))
    person, *_ = method(env, project='me', content='我偏好短而明确的答复', conditions=())
    settings(env, allow_remote=True)
    before = deepcopy(ACTIVE)
    response = snapshot(env)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['version'] == 'external-snapshot@1'
    assert value['project_id'] == 'alpha' and value['client'] == 'codex'
    assert value['generated_at'] and value['turn_id'].startswith('turn-')
    assert value['text'].startswith('<!-- chriptmas-memory:external-snapshot@1:begin -->\n')
    assert value['text'].endswith('\n<!-- chriptmas-memory:external-snapshot@1:end -->')
    result = value['result']
    assert {row['object_id'] for row in result['entries']} == {first.id, second.id}
    assert {row['object_id'] for row in result['profile']} == {person.id}
    assert all(row['conditions'] for row in result['entries'])
    assert fact.id not in value['text'] and foreign.id not in value['text']
    assert result['text'] in value['text']
    events = env.http.app.state.ai_turn_store.events_after(value['turn_id'])
    assert events[-1]['type'] == 'turn.completed'
    outcome = next(event for event in events if event['type'] == 'tool.outcome.recorded')
    assert env.http.app.state.ai_turn_store.effect_runner.log.get(outcome['correlation']['tool_call_id']).state is EffectState.SETTLED_OK
    assert len(env.records.list('v2_external_agent_deliveries')) == 1
    assert len(env.records.list('v2_external_agent_reservations')) == 1
    assert env.records.list('v2_external_agent_citations') == ()
    assert env.records.list('v2_usage_insight') == env.records.list('v2_usage_document') == ()
    assert env.model.calls == 0 and ACTIVE == before


@pytest.mark.parametrize('profile_enabled,private', [(True, False), (False, False), (True, True)])
def test_snapshot_profile_obeys_independent_external_switch_and_private_scope(env, profile_enabled, private):
    import sys
    import threading
    from backend.memory_app.v2.profile import confirmed_profile
    person, *_ = method(env, project='me', content='我只偏好合成测试内容', conditions=())
    settings(env, allow_remote=True, include_profile=profile_enabled)
    if private:
        set_private_project(env.records, 'me', True, 0)
    observed = []
    def calls(frame, event, _argument):
        if event == 'call' and frame.f_code is confirmed_profile.__code__:
            observed.append(True)
    previous_thread, previous_main = threading.getprofile(), sys.getprofile()
    try:
        threading.setprofile_all_threads(calls)
        response = snapshot(env)
    finally:
        threading.setprofile_all_threads(previous_thread)
        sys.setprofile(previous_main)
    assert response.status_code == 200, response.text
    rows = response.json()['result']['profile']
    if profile_enabled and not private:
        assert observed and {row['object_id'] for row in rows} == {person.id}
    else:
        assert observed == [] and rows == []
        assert env.records.list('v2_profile_blocks') == ()
        assert person.id not in response.json()['text']
    assert env.model.calls == 0


@pytest.mark.parametrize('blocked', ['off', 'client', 'private'])
def test_snapshot_blocked_admission_delivers_no_content_or_receipt(env, blocked):
    settings(env, allow_remote=blocked != 'off')
    if blocked == 'client':
        settings(env, clients={'codex': False, 'claude': True})
    elif blocked == 'private':
        set_private_project(env.records, 'alpha', True, 0)
    response = snapshot(env)
    assert response.status_code == 409
    assert set(response.json()) == {'detail'}
    assert env.records.list('v2_external_agent_deliveries') == ()
    assert env.records.list('v2_external_agent_reservations') == ()
    assert env.records.list('v2_profile_blocks') == () and env.model.calls == 0


def test_snapshot_budget_keeps_entries_atomic_and_still_records_delivery(env):
    method(env)
    settings(env, allow_remote=True)
    response = snapshot(env, budget=1)
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert result['budget'] == 1 and result['tokens'] == 0
    assert result['entries'] == result['profile'] == []
    assert len(env.records.list('v2_external_agent_deliveries')) == 1


@pytest.mark.parametrize('restriction', ['private_source', 'forgotten'])
def test_snapshot_omits_qualified_methods_after_source_policy_or_manual_forgetting(env, restriction):
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.v2.recall_preferences import set_preference
    from backend.recognition import WorkScope
    recognition, *_ = method(env)
    scope = WorkScope('local-user', 'alpha')
    if restriction == 'private_source':
        SourceEgressService(env.records).set_policy(scope, 'recognition', recognition.id,
            expected_source_revision=recognition.revision, expected_policy_revision=0, allowed_purposes=[])
    else:
        set_preference(env.records, scope, recognition.id, recognition_revision=recognition.revision,
            preference_revision=0, state='forgotten')
    settings(env, allow_remote=True)
    response = snapshot(env)
    assert response.status_code == 200, response.text
    assert response.json()['result']['entries'] == []
    assert recognition.id not in response.json()['text']
    assert len(env.records.list('v2_external_agent_deliveries')) == 1 and env.model.calls == 0


@pytest.mark.parametrize('changes', [{'client': 'unknown'}, {'budget': True}, {'project_id': '../other'}, {'confirm': True}])
def test_snapshot_http_arguments_do_not_bypass_scope_or_budget(env, changes):
    settings(env, allow_remote=True)
    response = snapshot(env, **changes)
    assert response.status_code == 400
    assert env.records.list('v2_external_agent_deliveries') == ()
