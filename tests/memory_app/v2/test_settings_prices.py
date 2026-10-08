import pytest

from tests.memory_app.v2.test_settings import env


RATES = {'input_per_million': '2', 'output_per_million': '8', 'cache_read_per_million': '0.04'}


def test_verified_official_model_prices_cannot_be_overridden_by_manual_api(env):
    app, client = env
    assert client.put('/api/recognition/settings', json={'purpose': 'generation',
        'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
        'api_key': 'test-private-value', 'allow_remote': True, 'expected_revision': 0}).status_code == 200
    before = app.state.recognition_records.read('recognition_model_config', 'generation')
    price = client.get('/api/v2/settings').json()['model']['generation']['pricing']
    assert price['editable'] is False and price['source'] == 'deepseek-cny-2026-10-04'
    response = client.patch('/api/v2/settings/model-prices', json={
        'purpose': 'generation', 'rates': RATES, 'expected_revision': 0,
        'expected_configuration_revision': 1})
    assert response.status_code == 400 and response.json()['detail'] == 'model_price_official_readonly'
    assert app.state.recognition_records.list('v2_model_prices') == ()
    assert app.state.recognition_records.read('recognition_model_config', 'generation') == before
    assert client.get('/api/v2/settings').json()['model']['generation']['pricing'] == price


def test_unknown_model_prices_are_nullable_and_saved_with_separate_cas(env):
    app, client = env
    assert client.put('/api/recognition/settings', json={'purpose': 'generation',
        'base_url': 'https://proxy.invalid/v1', 'model': 'unknown', 'api_key': 'test-private-value',
        'allow_remote': True, 'expected_revision': 0}).status_code == 200
    before = app.state.recognition_records.read('recognition_model_config', 'generation')
    price = client.get('/api/v2/settings').json()['model']['generation']['pricing']
    assert price['rates'] is None and price['editable'] is True and price['revision'] == 0
    body = {'purpose': 'generation', 'rates': RATES, 'expected_revision': 0, 'expected_configuration_revision': 1}
    saved = client.patch('/api/v2/settings/model-prices', json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()['rates'] == RATES and saved.json()['revision'] == 1
    assert client.patch('/api/v2/settings/model-prices', json=body).status_code == 409
    assert app.state.recognition_records.read('recognition_model_config', 'generation') == before
    assert 'test-private-value' not in saved.text
    assert client.patch('/api/v2/settings/model-prices', json={**body,
        'rates': {key: None for key in RATES}, 'expected_revision': 1}).status_code == 200


@pytest.mark.parametrize('change', [
    {'purpose': 'asr'}, {'expected_revision': True}, {'expected_configuration_revision': -1},
    {'rates': {**RATES, 'input_per_million': 'NaN'}}, {'rates': {**RATES, 'output_per_million': '-1'}},
    {'rates': {**RATES, 'cache_read_per_million': True}}, {'rates': {**RATES, 'currency': 'USD'}},
    {'api_key': 'never-store-this'},
])
def test_invalid_prices_do_not_create_sidecar_or_change_model_settings(env, change):
    app, client = env
    response = client.patch('/api/v2/settings/model-prices', json={
        'purpose': 'generation', 'rates': RATES, 'expected_revision': 0,
        'expected_configuration_revision': 0, **change})
    assert response.status_code == 400
    assert app.state.recognition_records.list('v2_model_prices') == ()
    assert app.state.recognition_records.read('recognition_model_config', 'generation') is None
