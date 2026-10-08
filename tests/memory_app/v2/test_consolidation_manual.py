from datetime import datetime, timezone, timedelta

import pytest
from fastapi import HTTPException

from backend.memory_app.v2.consolidation import Consolidation
from backend.memory_app.v2.policies import override
from tests.memory_app.v2.test_workbench_ask import env as _env

env = _env


def test_running_project_reuses_job_and_other_project_hits_same_global_budget(env):
    callbacks = []
    conso = Consolidation(env.records, env.service, env.documents, None)
    with override(trigger='@2', consolidate='@2'):
        first = conso.request('alpha', callbacks.append)
        assert conso.request('alpha', callbacks.append) == first
        assert len(callbacks) == 1
        with pytest.raises(HTTPException) as error:
            conso.request('beta', callbacks.append)
        assert error.value.status_code == 409 and error.value.detail == 'consolidate_limit'
        assert conso.status('alpha')['running'] is True
        assert conso.status('beta')['job_id'] is None
        callbacks[0]()
        assert conso.status('alpha')['limit'] is True
        with pytest.raises(HTTPException) as error:
            conso.request('alpha', callbacks.append)
        assert error.value.detail == 'consolidate_limit'
    assert env.records.list('v2_consolidation_runs')[0].payload['status'] == 'completed'


def test_status_route_is_strictly_read_only_and_manual_returns_only_job_id(env):
    before = {name: env.records.list(name) for name in ('v2_consolidation_runs', 'v2_learning_accumulation', 'v2_memory_turn_keys')}
    response = env.http.get('/api/v2/library/consolidate?project_id=alpha')
    assert response.status_code == 200
    assert response.json() == {'score': 0, 'limit': False, 'running': False, 'job_id': None}
    assert {name: env.records.list(name) for name in before} == before
    with override(trigger='@2', consolidate='@2'):
        response = env.http.post('/api/v2/library/consolidate', json={'project_id': 'alpha'})
    assert response.status_code == 200 and set(response.json()) == {'job_id'}
    assert env.http.post('/api/v2/library/consolidate', json={'project_id': 'alpha', 'extra': True}).status_code == 400
    assert env.http.post('/api/v2/library/consolidate', json={'project_id': 'beta'}).status_code == 409
    assert env.http.get('/api/v2/library/consolidate?project_id=beta').json()['job_id'] is None
    items = env.http.get('/api/v2/jobs?project_id=alpha').json()['items']
    row = next(item for item in items if item['id'] == response.json()['job_id'])
    assert row['kind'] == 'task' and row['target'] == {'type': 'task', 'id': row['id']}
    assert not any(item['id'] == row['id'] for item in env.http.get('/api/v2/jobs?project_id=beta').json()['items'])


def test_stale_same_day_job_is_fenced_before_its_replacement_is_exposed(env):
    instant = [datetime(2026, 5, 1, 12, tzinfo=timezone.utc)]
    conso = Consolidation(env.records, env.service, env.documents, None, now=lambda: instant[0])
    callbacks = []
    with override(trigger='@2', consolidate='@2'):
        first = conso.request('alpha', callbacks.append)
        instant[0] += timedelta(seconds=601)
        second = conso.request('alpha', callbacks.append)
        assert first != second and len(callbacks) == 2
        assert conso.status('alpha')['job_id'] == second['job_id']
        assert env.records.read('v2_consolidation_jobs', first['job_id']).payload['status'] == 'failed'
