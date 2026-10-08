import sqlite3

from core.storage_provider.connection_scope import _SCOPE
from tests.memory_app.v2.test_workbench_ask import env as ask_env, publish
from tests.memory_app.v2.test_workbench_remember import env as remember_env, post, wait


def test_library_projection_reuses_one_connection_through_timing_write(ask_env, monkeypatch):
    publish(ask_env)
    original, opened = sqlite3.connect, []
    def connect(*args, **kwargs):
        opened.append((args[0], _SCOPE.get()))
        return original(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    response = ask_env.http.get('/api/v2/library/insights', params={'project_id': 'alpha'})
    assert response.status_code == 200, response.text
    assert len(response.json()['items']) == 1
    assert len(opened) == 1
    assert opened[0][1] is not None and opened[0][1].ended


def test_remember_organization_and_insights_keep_one_background_unit(remember_env, monkeypatch):
    original, units = remember_env.model.complete, []
    def complete(*args, **kwargs):
        units.append(_SCOPE.get())
        return original(*args, **kwargs)
    monkeypatch.setattr(remember_env.model, 'complete', complete)
    result = post(remember_env)
    receipt = wait(remember_env, result)['receipt']['remember']
    assert receipt['state'] == 'done'
    assert len(units) >= 2 and units[0] is not None
    assert all(unit is units[0] for unit in units)
    assert units[0].ended
