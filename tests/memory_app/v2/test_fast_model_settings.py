"""The settings API writes only the auxiliary choice with independent CAS."""
import pytest

from tests.memory_app.v2.test_settings import env


def configure(client):
    assert client.put('/api/recognition/settings', json={'purpose': 'generation',
        'base_url': 'https://synthetic.invalid/v1', 'model': 'main', 'api_key': 'synthetic-only',
        'allow_remote': True, 'expected_revision': 0}).status_code == 200


def test_fast_settings_safe_projection_separate_cas_and_clear(env):
    app, client = env
    configure(client)
    original = app.state.recognition_records.read('recognition_model_config', 'generation')
    public = client.get('/api/v2/settings').json()['model']
    body = {'model': 'quick', 'expected_revision': 0, 'expected_generation_revision': 1,
            'expected_mode_revision': public['generation_mode']['revision']}
    saved = client.patch('/api/v2/settings/fast-model', json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json() == {'model': 'quick', 'revision': 1, 'configured': True}
    assert 'synthetic-only' not in saved.text
    assert client.patch('/api/v2/settings/fast-model', json=body).status_code == 409
    assert app.state.recognition_records.read('recognition_model_config', 'generation') == original
    assert client.get('/api/v2/settings').json()['model']['generation']['fast_model'] == saved.json()
    cleared = client.patch('/api/v2/settings/fast-model', json={**body, 'model': None, 'expected_revision': 1})
    assert cleared.status_code == 200 and cleared.json()['configured'] is False
    assert app.state.recognition_records.read('recognition_model_config', 'generation') == original


@pytest.mark.parametrize('change', [{'model': ''}, {'expected_revision': True},
    {'expected_mode_revision': -1}, {'api_key': 'never-accept-key'}, {'model': ['quick']}])
def test_invalid_fast_settings_never_write(env, change):
    app, client = env
    configure(client)
    response = client.patch('/api/v2/settings/fast-model', json={'model': 'quick', 'expected_revision': 0,
        'expected_generation_revision': 1, 'expected_mode_revision': 0, **change})
    assert response.status_code == 400
    assert app.state.recognition_records.list('v2_generation_fast_model') == ()
