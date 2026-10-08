"""The task profile and the ensuing frozen request select the same versions."""
import asyncio
import copy

import pytest

from backend.memory_app.v2.policies import ACTIVE, get, override, register
from backend.memory_app.v2.task_do import TaskDo, TASK_EXECUTIONS
from tests.memory_app.v2.test_workbench_ask import env, publish


@pytest.fixture(scope='module', autouse=True)
def alternatives():
    for interface in ('compose', 'strength'):
        register(interface, '@9902')(get(interface, version='@1'))


@pytest.mark.parametrize('interface', ['compose', 'strength'])
def test_real_task_profile_selection_is_frozen_before_active_changes(env, monkeypatch, interface):
    from backend.memory_app.v2.profile import frozen_task_profile
    persona, _ = publish(env, 'I prefer precise answers', project='me')
    from backend.memory_app.v2.usage import UsageService
    UsageService(env.records).initialize('insight', persona.id, 'me')
    seen = []

    @register(interface, '@9901')
    def change_active(*args, **kwargs):
        seen.append('@9901')
        monkeypatch.setitem(ACTIVE, interface, '@9902')
        return get(interface, version='@1')(*args, **kwargs)

    monkeypatch.setitem(ACTIVE, interface, '@9901')
    task = TaskDo(env.records, env.model, None, None, None, None)
    receipt, state = task.initial('turn-task-policy', 'alpha', 'Write a draft', None)
    request = state['request']
    assert seen and set(seen) == {'@9901'}
    assert ACTIVE[interface] == '@9902'
    assert request['policy_versions'][interface] == '@9901'
    profile = frozen_task_profile(env.records, env.service, request)
    assert profile['items'][0]['id'] == persona.id
    assert 'I prefer precise answers' in profile['text']
    assert env.model.calls == 0


@pytest.mark.parametrize('historical', [False, True])
def test_task_resume_preserves_saved_request_and_profile_instead_of_preparing_again(env, historical):
    from backend.memory_app.v2.profile import frozen_task_profile
    publish(env, 'I prefer precise answers', project='me')
    received = []

    class Organization:
        def start(self, request, **options):
            received.append(copy.deepcopy(request))
            assert frozen_task_profile(env.records, env.service, request)['count'] == 1

    task = TaskDo(env.records, env.model, None, Organization(),
        lambda *args: {'status': 'running'}, lambda *args: [])
    receipt, state = task.initial('turn-task-resume', 'alpha', 'Write a draft', None)
    if historical:
        state['request'].pop('policy_versions')
    before = copy.deepcopy(state['request'])
    profile = env.records.read('v2_task_profiles', before['turn_id'])
    with env.records.begin() as tx:
        tx.put(TASK_EXECUTIONS, 'turn-task-resume', state, expected_revision=0)
        tx.put('v2_turns', 'turn-task-resume', {'receipt': {'do': receipt}}, expected_revision=0)
        tx.commit()
    with override(compose='@9902', strength='@9902'):
        asyncio.run(task.advance('turn-task-resume'))
    saved = env.records.read(TASK_EXECUTIONS, 'turn-task-resume')
    assert received == [before]
    assert saved.payload['request'] == before
    assert saved.payload['started']
    assert env.records.read('v2_task_profiles', before['turn_id']) == profile
    assert env.model.calls == 0
