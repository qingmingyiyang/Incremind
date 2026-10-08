"""Provider storage is an independent, default-off selection with current CAS."""
import sqlite3

import pytest

from tests.memory_app.v2.test_settings import env


COLLECTION = 'v2_provider_store_settings'
URL = '/api/v2/settings/provider-store'


def configure(client, *, base='https://api.openai.com/v1', revision=0, model='synthetic'):
    response = client.put('/api/recognition/settings', json={'purpose': 'generation',
        'base_url': base, 'model': model, 'api_key': 'synthetic-only', 'enabled': True,
        'allow_remote': True, 'expected_revision': revision})
    assert response.status_code == 200, response.text


def body(view, enabled=True):
    return {'enabled': enabled, 'expected_revision': view['revision'],
        'expected_generation_revision': view['generation_revision'],
        'expected_mode_revision': view['mode_revision']}


def test_default_off_get_never_creates_a_selection(env):
    app, client = env
    assert client.get(URL).json() == {'available': False, 'enabled': False,
        'revision': 0, 'generation_revision': 0, 'mode_revision': 0}
    configure(client)
    view = client.get(URL)
    assert view.status_code == 200
    assert view.json() == {'available': True, 'enabled': False,
        'revision': 0, 'generation_revision': 1, 'mode_revision': 0}
    assert app.state.recognition_records.list(COLLECTION) == ()


def test_selection_cas_preserves_complete_public_snapshot_and_main_record(env):
    app, client = env
    configure(client)
    models, records = app.state.recognition_models, app.state.recognition_records
    original = records.read('recognition_model_config', 'generation')
    public, snapshot = models.public(), models.snapshot('generation')
    loader, subscription = models._completion_fn, models._responses
    initial = client.get(URL).json()
    selected = client.put(URL, json=body(initial))
    assert selected.status_code == 200, selected.text
    assert selected.json() == {**initial, 'enabled': True, 'revision': 1}
    assert client.put(URL, json=body(initial)).status_code == 409
    cleared = client.put(URL, json=body(selected.json(), False))
    assert cleared.status_code == 200
    assert cleared.json() == {**initial, 'revision': 2}
    assert records.read('recognition_model_config', 'generation') == original
    assert models.public() == public and models.snapshot('generation') == snapshot
    assert models._completion_fn is loader and models._responses is subscription
    assert 'synthetic-only' not in selected.text
    assert 'secret_ref' not in str(records.read(COLLECTION, 'default').payload)


def test_configuration_drift_projects_off_without_rewriting_selection(env):
    app, client = env
    configure(client)
    old = client.get(URL).json()
    assert client.put(URL, json=body(old)).status_code == 200
    selection = app.state.recognition_records.read(COLLECTION, 'default')
    configure(client, revision=1, model='changed')
    current = client.get(URL).json()
    assert current == {**old, 'enabled': False, 'revision': 1, 'generation_revision': 2}
    assert app.state.recognition_records.read(COLLECTION, 'default') == selection
    assert client.put(URL, json={**body(current), 'expected_generation_revision': 1}).status_code == 409
    assert app.state.recognition_records.read(COLLECTION, 'default') == selection


def test_mode_drift_and_stale_cas_never_reinterpret_old_selection(env):
    app, client = env
    configure(client)
    initial = client.get(URL).json()
    assert client.put(URL, json=body(initial)).status_code == 200
    selection = app.state.recognition_records.read(COLLECTION, 'default')
    app.state.recognition_models.update_generation_mode(mode='api', local_enabled=False,
        local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=0)
    current = client.get(URL).json()
    assert current == {**initial, 'revision': 1, 'mode_revision': 1}
    assert client.put(URL, json={**body(current), 'expected_mode_revision': 0}).status_code == 409
    assert app.state.recognition_records.read(COLLECTION, 'default') == selection


@pytest.mark.parametrize('change', [{'enabled': 1}, {'enabled': None},
    {'expected_revision': True}, {'expected_generation_revision': -1},
    {'expected_mode_revision': '0'}, {'api_key': 'rejected'}, {'store': True}])
def test_invalid_input_has_zero_selection_writes(env, change):
    app, client = env
    configure(client)
    response = client.put(URL, json={**body(client.get(URL).json()), **change})
    assert response.status_code == 400
    assert app.state.recognition_records.list(COLLECTION) == ()


@pytest.mark.parametrize('base', ['https://proxy.example/v1', 'https://api.openai.com.evil/v1',
    'https://api.openai.com/v1/other', 'http://127.0.0.1:8001/v1'])
def test_unproven_api_or_default_local_cannot_enable_storage(env, base):
    app, client = env
    configure(client, base=base)
    current = client.get(URL).json()
    assert current['available'] is False and current['enabled'] is False
    response = client.put(URL, json=body(current))
    assert response.status_code == 400 and response.json()['detail'] == 'provider_store_unavailable'
    assert app.state.recognition_records.list(COLLECTION) == ()


def test_sql_failure_rolls_back_selection_without_touching_configuration(env):
    app, client = env
    configure(client)
    records = app.state.recognition_records
    original = records.read('recognition_model_config', 'generation')
    with sqlite3.connect(records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_provider_store BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection = 'v2_provider_store_settings' BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic'):
        client.put(URL, json=body(client.get(URL).json()))
    assert records.list(COLLECTION) == ()
    assert records.read('recognition_model_config', 'generation') == original
