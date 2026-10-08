"""Fast choices reuse the actual gateway, subscription and fixed local identity."""
import json

import httpx
import pytest

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from tests.memory_app.test_fast_generation import configured, choose
from tests.memory_app.test_governed_generation import Control, Metadata, WireSink, _route
from tests.memory_app.test_server_local_model_identity import local_application
from tests.memory_app.v2.test_chatgpt_subscription import env as subscription_env, login


def test_fast_budget_comes_from_selected_gateway_and_main_manual_price_is_not_reused(tmp_path):
    models, _ = configured(tmp_path)
    models.update_model_prices('generation', {'input_per_million': '2', 'output_per_million': '8',
        'cache_read_per_million': '0.04'}, expected_revision=0, expected_configuration_revision=1)
    choose(models, 'quick')
    selected = models.for_auxiliary(models.freeze_auxiliary_binding())
    gateway = selected._generation_gateway(selected.snapshot('generation'))
    assert gateway._model == 'openai/quick'
    assert selected.generation_budget_limits(expected_revision=1, max_tokens=512) == gateway.input_budget_limits(max_tokens=512)
    assert selected.model_prices('generation')['rates'] is None
    assert models.model_prices('generation')['source'] == 'manual'
    assert selected.snapshot('generation')['model'] == 'quick'


def test_fast_subscription_uses_catalog_and_native_responses_without_api_fallback(subscription_env, tmp_path):
    service, _, secrets, records = subscription_env
    login(subscription_env)
    config = ModelConfiguration(records, tmp_path, secrets=secrets, subscriptions=service)
    config.update('generation', {'model': 'api-main', 'base_url': 'https://synthetic.invalid/v1',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    config.select_subscription(model='gpt-fixture', expected_revision=0)
    with pytest.raises(ModelConfigurationError, match='subscription_model_unavailable'):
        choose(config, 'not-in-catalog')
    assert records.list('v2_generation_fast_model') == ()
    choose(config, 'gpt-fixture')
    selected = config.for_auxiliary(config.freeze_auxiliary_binding())
    route = _route(selected)
    route['configuration']['subscription_binding'] = selected.public()['generation']['subscription_binding']
    wires = []
    def wire(request):
        wires.append(json.loads(request.content))
        assert request.url == 'https://api.openai.com/v1/responses'
        response = {'status': 'completed', 'output': [{'type': 'message', 'content':
            [{'type': 'output_text', 'text': 'accepted'}]}],
            'usage': {'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5}}
        events = [{'type': 'response.output_text.delta', 'delta': 'accepted'},
                  {'type': 'response.completed', 'response': response}]
        return httpx.Response(200, text=''.join('data: ' + json.dumps(event) + '\n\n' for event in events),
            headers={'content-type': 'text/event-stream'})
    with httpx.Client(transport=httpx.MockTransport(wire)) as client:
        config._responses.client = client
        output, metadata = selected.complete_governed([{'role': 'user', 'content': 'Synthetic'}],
            routing_snapshot=route, execution_control=Control(), metadata_sink=Metadata(),
            wire_attempt_sink=WireSink(), purpose='aux')
    assert output == 'accepted' and metadata['model'] == 'gpt-fixture'
    assert [body['model'] for body in wires] == ['gpt-fixture']
    service.logout(expected_revision=service.status()['revision'])
    with pytest.raises(ModelConfigurationError):
        selected.snapshot('generation')
    assert len(wires) == 1


def test_fast_local_choice_keeps_fixed_profile_and_exact_internal_endpoint(tmp_path, monkeypatch):
    config, auth, client, inference, keys = local_application(tmp_path, monkeypatch, 8765)
    primary = config.records.read('recognition_model_config', 'generation')
    with pytest.raises(ModelConfigurationError, match='fast_local_model_unavailable'):
        choose(config, 'other-model')
    choose(config, 'qwen2.5-1.5b-instruct')
    selected = config.for_auxiliary(config.freeze_auxiliary_binding())
    route = _route(selected)
    route['execution_location'] = 'local_loopback'
    output, metadata = selected.complete_governed([{'role': 'user', 'content': 'Synthetic'}],
        routing_snapshot=route, execution_control=Control(), metadata_sink=Metadata(),
        wire_attempt_sink=WireSink(), purpose='aux')
    assert output == 'done' and metadata['model'] == 'qwen2.5-1.5b-instruct'
    assert len(inference) == 1 and keys[0] != 'local-model'
    assert client.get('/api/private', headers={'Authorization': 'Bearer ' + keys[0]}).status_code == 401
    assert auth.internal_key_for('http://127.0.0.1:8001/local-model/v1') is None
    assert config.records.read('recognition_model_config', 'generation') == primary


@pytest.mark.parametrize('change', ['primary', 'mode'])
def test_changed_parent_authority_rejects_frozen_aux_before_any_wire(tmp_path, change):
    models, wires = configured(tmp_path)
    choose(models, 'quick')
    selected = models.for_auxiliary(models.freeze_auxiliary_binding())
    route = _route(selected)
    if change == 'primary':
        models.update('generation', {'model': 'new-main', 'expected_revision': 1})
    else:
        models.update_generation_mode(mode='api', local_enabled=False,
            local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=0)
    with pytest.raises(ModelConfigurationError):
        selected.complete_governed([{'role': 'user', 'content': 'Synthetic'}], routing_snapshot=route,
            execution_control=Control(), metadata_sink=Metadata(), wire_attempt_sink=WireSink(), purpose='aux')
    assert wires == []


def test_subscription_account_change_during_catalog_read_cannot_save_choice(subscription_env, tmp_path):
    service, _, secrets, records = subscription_env
    login(subscription_env)
    config = ModelConfiguration(records, tmp_path, secrets=secrets, subscriptions=service)
    config.update('generation', {'model': 'main', 'base_url': 'https://synthetic.invalid/v1',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    config.select_subscription(model='gpt-fixture', expected_revision=0)
    original = service.client
    def wire(request):
        if request.url.path == '/v1/models':
            service.logout(expected_revision=service.status()['revision'])
            return httpx.Response(200, json={'models': [{'slug': 'gpt-fixture', 'display_name': 'Fixture', 'visibility': 'list'}]})
        return original.send(request)
    with httpx.Client(transport=httpx.MockTransport(wire)) as client:
        service.client = client
        with pytest.raises(ModelConfigurationError, match='fast_model_configuration_changed'):
            choose(config, 'gpt-fixture')
    service.client = original
    assert records.list('v2_generation_fast_model') == ()
