from copy import deepcopy
import json
import sqlite3

import pytest

from backend.memory_app.v2.policies import override
from tests.memory_app.v2.test_context_feedback import env as _env
from tests.memory_app.v2.test_situation_methods import method
from tests.memory_app.v2.test_workbench_ask import ask, publish

env = _env


def actual_feedback(env):
    one = method(env, '留出换货余地', ['挑礼物时'])
    response = ask(env, text='给小王选生日礼物？', intent='ask')
    assert response.status_code == 200
    turn = response.json()['turn']
    body = {'project_id': 'alpha', 'object_kind': 'recognition', 'object_id': one.id,
            'object_revision': 1, 'expected_revision': 0}
    return turn, body, f"/api/v2/workbench/turns/{turn['id']}/context-feedback"


def test_real_strike_freezes_consumed_method_and_is_idempotent_without_rewriting_turn(env):
    with override(retrieve='@3', compose='@3'):
        turn, body, url = actual_feedback(env)
    original = deepcopy(env.records.read('v2_turns', turn['id']))
    saved = env.http.post(url, json=body)
    assert saved.status_code == 200
    assert env.http.post(url, json=body).json() == saved.json()
    events = env.records.list('v2_correction_events')
    assert len(events) == 1
    event = events[0].payload
    assert event['type'] == 'strike' and event['turn_id'] == turn['id']
    assert json.loads(event['before']) == {'text': '留出换货余地', 'conditions': ['挑礼物时']}
    assert event['after'] == '' and event['source_refs']
    assert env.records.read('v2_turns', turn['id']) == original


def test_correction_insert_failure_rolls_back_the_actual_feedback(env):
    with override(retrieve='@3', compose='@3'):
        turn, body, url = actual_feedback(env)
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_strike_correction BEFORE INSERT ON crp_structured_records WHEN NEW.collection = 'v2_correction_events' BEGIN SELECT RAISE(ABORT, 'synthetic strike correction conflict'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic strike correction conflict'):
        env.http.post(url, json=body)
    assert env.records.list('v2_context_feedback') == ()
    assert env.records.list('v2_correction_events') == ()


def test_persona_and_ordinary_fact_keep_the_actual_method_citation_number(env):
    persona = method(env, '先保留一个可验证的小步骤', ['挑礼物时'], project='me')
    fact, _ = publish(env, '小王喜欢蓝色生日礼物')
    with override(retrieve='@3', compose='@3'):
        turn, body, url = actual_feedback(env)
    entries = turn['receipt']['ask']['context']['entries']
    assert entries[0]['id'] == persona.id and entries[0]['persona'] is True
    assert entries[1]['id'] == fact.id and entries[1]['persona'] is False
    assert entries[2]['id'] == body['object_id'] and entries[2]['supplemented'] is True
    citation = next(row for row in turn['receipt']['ask']['citations'] if row['id'] == body['object_id'])
    assert citation['n'] == 2
    frozen = env.http.app.state.ai_turn_store.get_immutable_payload(turn['id'], 'answer-model-input-answer')[1]
    assert '情境补全方法：\n[2]' in frozen['messages'][-1]['content']
    original = deepcopy(env.records.read('v2_turns', turn['id']))
    assert env.http.post(url, json=body).status_code == 200
    events = env.records.list('v2_correction_events')
    assert len(events) == 1 and events[0].payload['object_id'] == body['object_id']
    assert json.loads(events[0].payload['before']) == {'text': '留出换货余地', 'conditions': ['挑礼物时']}
    assert env.records.read('v2_turns', turn['id']) == original
