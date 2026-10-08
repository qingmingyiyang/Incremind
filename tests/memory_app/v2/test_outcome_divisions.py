"""Real HTTP division adjustment and the original SQLite sample transaction."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
import sqlite3
from threading import Barrier
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.memory_app.v2.task_divisions import TaskDivisions
from tests.memory_app.test_api import FakeModels, _shutdown

COLLECTION = 'v2_outcome_corrections'
OLD = [{'goal': '核对资料', 'deliverable': '摘要', 'capabilities': ['memory.recall'], 'depends_on': []},
       {'goal': '完成方案', 'deliverable': '整理稿', 'capabilities': ['document.draft.propose'], 'depends_on': [0]}]
NEW = [{**OLD[0], 'goal': '先比较证据'}, {**OLD[1], 'goal': '再完成方案'}]


@pytest.fixture
def division_env(tmp_path, monkeypatch):
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    module = importlib.import_module('backend.memory_app.app')
    models = FakeModels()
    app = module.create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=models)
    client = TestClient(app, raise_server_exceptions=False)
    records = app.state.recognition_records
    samples = TaskDivisions(records, models=models)
    identity = 'turn-outcome-division'
    samples.complete(identity, project='project-a', text='核对资料再完成方案', items=deepcopy(OLD), outcome='done')
    # The HTTP scope row is not Kernel completion evidence; this leaf tests the sample owner.
    with records.begin() as tx:
        tx.put('v2_turns', identity, {'project_id': 'project-a'}, expected_revision=0)
        tx.commit()
    yield SimpleNamespace(client=client, records=records, samples=samples, models=models, identity=identity,
        endpoint='/api/v2/workbench/turns/' + identity + '/division')
    _shutdown(client)


def adjust(env, *, items=NEW, revision=1, project='project-a'):
    return env.client.patch(env.endpoint, json={'project_id': project,
        'items': deepcopy(items), 'expected_revision': revision})


def test_http_adjust_records_actual_ordered_goals_and_preserves_full_sample(division_env):
    env = division_env
    before = env.records.read('v2_task_divisions', env.identity)
    response = adjust(env)
    assert response.status_code == 200, response.text
    saved = env.records.read('v2_task_divisions', env.identity)
    assert saved.revision == 2
    assert response.json() == {**saved.payload, 'revision': saved.revision}
    assert set(saved.payload) == set(before.payload)
    assert saved.payload['items'] == NEW and saved.payload['adjusted'] is True
    assert saved.payload['adjusted_at']
    assert {key: value for key, value in saved.payload.items()
        if key not in {'items', 'adjusted', 'adjusted_at'}} == {key: value for key, value in before.payload.items()
        if key not in {'items', 'adjusted', 'adjusted_at'}}
    event, = env.records.list(COLLECTION)
    assert event.payload['kind'] == 'division_adjust' and event.payload['project_id'] == 'project-a'
    assert event.payload['turn_id'] == env.identity
    assert event.payload['before_goals'] == [item['goal'] for item in before.payload['items']]
    assert event.payload['after_goals'] == [item['goal'] for item in saved.payload['items']]
    assert event.payload['division_from_revision'] == 1 and event.payload['division_to_revision'] == 2
    assert event.payload['created_at'] == saved.payload['adjusted_at']
    assert event.revision == 1 and env.models.calls == []


def test_each_successful_adjust_same_goals_still_records_one_fact(division_env):
    env = division_env
    assert adjust(env, items=OLD).status_code == 200
    first, = env.records.list(COLLECTION)
    assert first.payload['before_goals'] == first.payload['after_goals'] == [item['goal'] for item in OLD]
    assert adjust(env, items=OLD, revision=2).status_code == 200
    rows = env.records.list(COLLECTION)
    assert len(rows) == 2 and len({row.object_id for row in rows}) == 2
    assert env.records.read(COLLECTION, first.object_id) == first
    latest = next(row for row in rows if row.object_id != first.object_id)
    assert latest.payload['division_from_revision'] == 2 and latest.payload['division_to_revision'] == 3
    assert latest.payload['before_goals'] == latest.payload['after_goals'] == [item['goal'] for item in OLD]
    assert env.samples.read(env.identity, 'project-a')['revision'] == 3


@pytest.mark.parametrize('failure,status', [('stale', 409), ('cross_project', 404), ('deleted', 400)])
def test_rejected_adjust_keeps_original_sample_and_all_fact_rows(division_env, failure, status):
    env = division_env
    if failure == 'stale':
        assert adjust(env).status_code == 200
    elif failure == 'deleted':
        deleted = env.client.request('DELETE', env.endpoint, json={
            'project_id': 'project-a', 'expected_revision': 1})
        assert deleted.status_code == 200, deleted.text
    before = env.records.list_all()
    rejected = adjust(env, project='project-b' if failure == 'cross_project' else 'project-a')
    assert rejected.status_code == status, rejected.text
    assert env.records.list_all() == before


@pytest.mark.parametrize('items', [[], [{**OLD[0], 'goal': ' '}],
    [{**OLD[0], 'depends_on': [1]}, {**OLD[1], 'depends_on': [0]}]])
def test_original_item_validator_rejects_without_new_facts(division_env, items):
    before = division_env.records.list_all()
    response = adjust(division_env, items=items)
    assert response.status_code == 400, response.text
    assert division_env.records.list_all() == before


def test_original_complete_and_delete_do_not_record_adjustment(division_env):
    env = division_env
    before = env.records.read('v2_task_divisions', env.identity)
    env.samples.complete(env.identity, project='project-a', text='另一次完成', items=deepcopy(NEW), outcome='partial')
    assert env.records.read('v2_task_divisions', env.identity) == before
    assert env.records.list(COLLECTION) == ()
    deleted = env.client.request('DELETE', env.endpoint, json={'project_id': 'project-a', 'expected_revision': 1})
    assert deleted.status_code == 200, deleted.text
    assert env.records.read('v2_task_divisions', env.identity).payload['deleted'] is True
    assert env.records.list(COLLECTION) == ()
    assert env.samples.similar('project-a', '核对资料再完成方案') == []


def test_private_project_adjust_records_only_local_fact(division_env):
    from backend.memory_app.v2.privacy import set_private_project
    env = division_env
    set_private_project(env.records, 'project-a', True, 0)
    response = adjust(env)
    assert response.status_code == 200, response.text
    event, = env.records.list(COLLECTION)
    assert event.payload['project_id'] == 'project-a' and event.payload['after_goals'] == [item['goal'] for item in NEW]
    assert env.models.calls == []
    assert env.samples.similar('project-a', '核对资料再完成方案') == []


@pytest.mark.parametrize('collection', [COLLECTION, 'v2_task_divisions'])
def test_sql_failure_rolls_back_sample_and_event_same_transaction(division_env, collection):
    env = division_env
    before = env.records.list_all()
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_division_fact BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='" + collection + "' BEGIN SELECT RAISE(ABORT, 'synthetic division failure'); END")
        connection.execute("CREATE TRIGGER reject_division_change BEFORE UPDATE ON crp_structured_records "
            "WHEN NEW.collection='" + collection + "' BEGIN SELECT RAISE(ABORT, 'synthetic division failure'); END")
    response = adjust(env)
    assert response.status_code == 500, response.text
    assert env.records.list_all() == before
    assert env.samples.read(env.identity, 'project-a')['revision'] == 1
    assert env.records.list(COLLECTION) == ()


def test_concurrent_adjust_only_cas_winner_writes_sample_and_event(division_env):
    env = division_env
    barrier = Barrier(2)
    def save(goal):
        barrier.wait(timeout=5)
        return adjust(env, items=[{**OLD[0], 'goal': goal}, OLD[1]])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, goal) for goal in ('并发一', '并发二')]
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    event, = env.records.list(COLLECTION)
    current = env.records.read('v2_task_divisions', env.identity)
    assert current.revision == 2
    assert event.payload['division_from_revision'] == 1 and event.payload['division_to_revision'] == 2
    assert event.payload['before_goals'] == [item['goal'] for item in OLD]
    assert event.payload['after_goals'] == [item['goal'] for item in current.payload['items']]
