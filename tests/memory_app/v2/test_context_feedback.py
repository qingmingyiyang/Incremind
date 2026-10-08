"""A strike is a user fact, never a rewrite of a frozen answer."""
from copy import deepcopy

import pytest
from fastapi import HTTPException

from backend.memory_app.v2.context_feedback import COLLECTION, strike
from backend.memory_app.v2.policies import override
from backend.memory_app.v2.task_do import TaskDo
from backend.recognition import WorkScope
from tests.memory_app.v2.test_situation_methods import method
from tests.memory_app.v2.test_workbench_ask import env as _env, ask

env = _env


@pytest.fixture(autouse=True)
def method_versions():
    with override(retrieve='@2', compose='@2'):
        yield


def test_real_ask_marks_frozen_methods_and_strike_preserves_history_and_receipts(env):
    one = method(env, '留出换货余地', ['挑礼物时'])
    response = ask(env, text='给小王选生日礼物？', intent='ask')
    assert response.status_code == 200, response.text
    result = response.json()
    turn = result['turn']
    receipt = turn['receipt']['ask']
    entries = receipt['context']['entries']
    assert len(entries) == 1 and entries[0]['id'] == one.id and entries[0]['supplemented'] is True
    assert entries[0]['object_revision'] == 1
    store = env.http.app.state.ai_turn_store
    frozen = store.get_immutable_payload(turn['id'], 'answer-model-input-answer')[1]
    assert '情境补全方法：' in frozen['messages'][-1]['content']
    assert '留出换货余地' in frozen['messages'][-1]['content']
    before = deepcopy(env.records.read('v2_turns', turn['id']))
    egress = deepcopy(env.records.read('workspace_ask_receipts', receipt['egress_receipt_id']))
    data = {'project_id': 'alpha', 'object_kind': 'recognition', 'object_id': one.id,
            'object_revision': 1, 'expected_revision': 0}
    url = f"/api/v2/workbench/turns/{turn['id']}/context-feedback"
    saved = env.http.post(url, json=data)
    assert saved.status_code == 200, saved.text
    assert env.http.post(url, json=data).json() == saved.json()
    row = env.records.read(COLLECTION, saved.json()['id'])
    assert row.revision == 1 and row.payload == {'turn_id': turn['id'], 'project_id': 'alpha',
        'object_kind': 'recognition', 'object_id': one.id, 'object_revision': 1, 'action': 'strike', 'at': row.payload['at']}
    assert env.records.read('v2_turns', turn['id']) == before
    assert env.records.read('workspace_ask_receipts', receipt['egress_receipt_id']) == egress
    assert env.http.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=alpha").json()['turns'][0]['receipt'] == turn['receipt']
    assert env.http.get(url + '?project_id=beta').status_code == 404
    assert env.http.post(url, json={**data, 'object_revision': 2}).status_code == 409
    assert env.model.calls >= 1


def test_struck_method_sorts_last_on_later_turn_and_other_project_stays_independent(env):
    one = method(env, '留出换货余地', ['挑礼物时'])
    turn_id = 'turn-feedback-test'
    with env.records.begin() as tx:
        tx.put('v2_turns', turn_id, {'project_id': 'alpha', 'receipt': {'ask': {'context': {'entries': [
            {'id': one.id, 'object_revision': 1, 'supplemented': True}]}}}}, expected_revision=0)
        tx.commit()
    strike(env.records, turn_id, project_id='alpha', object_kind='recognition',
           object_id=one.id, object_revision=1, expected_revision=0)
    for body in ('比较实用程度', '考虑收纳空间', '核实尺寸颜色'):
        method(env, body, ['挑礼物时'])
    plan = env.domains.query.prepare_ask('alpha', '给小王选生日礼物？')
    assert len([row for row in plan['chosen'] if row.get('supplemented')]) == 3
    assert one.id not in {row['id'] for row in plan['chosen']}
    with pytest.raises(HTTPException) as error:
        strike(env.records, turn_id, project_id='beta', object_kind='recognition',
               object_id=one.id, object_revision=1, expected_revision=0)
    assert error.value.status_code == 404


def test_task_initial_freezes_methods_alongside_profile_as_distinct_sidecars(env):
    one = method(env, '留出换货余地', ['挑礼物时'])
    class Organization:
        method_query = env.domains.query
    task = TaskDo(env.records, env.model, None, Organization(), None, None)
    receipt, state = task.initial('turn-task-methods', 'alpha', '给小王选生日礼物', None)
    request = state['request']
    assert {'type': 'recognition', 'id': one.id, 'revision': 1, 'project_id': 'alpha'} in request['privacy']['material_refs']
    row = env.records.read('v2_task_methods', request['turn_id'])
    assert row.payload['turn_id'] == 'turn-task-methods'
    assert row.payload['methods'][0]['entry']['id'] == one.id
    assert env.records.read('v2_task_profiles', request['turn_id']).payload['profile']['text'] == ''
    assert env.model.calls == 0
    with env.records.begin() as tx:
        tx.put('v2_turns', 'turn-task-methods', {'project_id': 'alpha', 'receipt': {'do': receipt}}, expected_revision=0)
        tx.commit()
    with pytest.raises(HTTPException) as error:
        strike(env.records, 'turn-task-methods', project_id='alpha', object_kind='recognition',
               object_id=one.id, object_revision=1, expected_revision=0)
    assert error.value.status_code == 409
    from backend.memory_app.v2.method_context import frozen_task_methods
    frozen = frozen_task_methods(env.records, env.domains.query, request)
    assert frozen['count'] == 1 and '留出换货余地' in frozen['text']
    from backend.memory_app.v2.projects import assign_scene
    assign_scene(env.records, 'recognition', one.id, 'alpha', '小李')
    # With no scene requested this remains eligible; exact source still guards revisions.
    assert frozen_task_methods(env.records, env.domains.query, request) == frozen
    env.service.revise(scope=WorkScope('local-user', 'alpha'), recognition_id=one.id,
        content='先核实售后期限', conditions=['挑礼物时'], expected_revision=1)
    from backend.recognition import RecognitionError
    with pytest.raises(RecognitionError):
        frozen_task_methods(env.records, env.domains.query, request)


def test_task_profile_method_retains_its_original_me_authority(env):
    persona = method(env, '先保留一个可验证的小步骤', ['挑礼物时'], project='me')
    local = method(env, '留出换货余地', ['挑礼物时'])
    class Organization:
        method_query = env.domains.query
    task = TaskDo(env.records, env.model, None, Organization(), None, None)
    _, state = task.initial('turn-profile-methods', 'alpha', '给小王选生日礼物', None)
    request = state['request']
    assert {tuple(sorted(item.items())) for item in request['privacy']['material_refs']} == {
        tuple(sorted({'type': 'recognition', 'id': persona.id, 'revision': 1, 'project_id': 'me'}.items())),
        tuple(sorted({'type': 'recognition', 'id': local.id, 'revision': 1, 'project_id': 'alpha'}.items()))}
    assert {row['id'] for row in env.records.read('v2_task_profiles', request['turn_id']).payload['profile']['items']} == {persona.id}
    assert {row['id'] for row in env.records.read('v2_task_methods', request['turn_id']).payload['methods']} == {local.id}
    assert env.model.calls == 0
