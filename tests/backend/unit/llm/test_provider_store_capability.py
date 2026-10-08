"""Only a real declared Responses adapter qualifies for optional storage."""
import httpx
import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.shared.llm.openai_responses import ResponsesCompletion
from core.storage_provider import SQLiteStructuredRecordStore
from tests.backend.unit.llm.test_model_transport_watchdog import Answer
from tests.backend.unit.llm.test_provider_background import BackgroundProvider


def configured(tmp_path, *, base='https://api.openai.com/v1', native=None):
    models = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3'),
        tmp_path, InMemorySecretStore(), completion_fn=native)
    models.update('generation', {'base_url': base, 'model': 'arbitrary-not-a-capability',
        'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    return models


def api_mode(models):
    # The original API-mode owner restores its saved profile. Store this same
    # synthetic API profile through its documented structured-record owner.
    original = models.records.read('recognition_model_config', 'generation')
    with models.records.begin() as tx:
        tx.put('recognition_generation_mode', 'default', {'mode': 'api', 'local_enabled': False,
            'local_base_url': 'http://127.0.0.1:8001/local-model/v1', 'api_config': original.payload}, expected_revision=0)
        tx.commit()


def test_exact_official_api_constructs_real_capability_without_replacing_old_loader(tmp_path):
    models = configured(tmp_path)
    loader, subscription = models._completion_fn, models._responses
    public, snapshot = models.public(), models.snapshot('generation')
    capability = models.provider_store_capability()
    adapter = models.provider_store_adapter()
    assert capability['available'] is True
    assert isinstance(adapter, ResponsesCompletion)
    assert adapter.api_base == 'https://api.openai.com/v1' and adapter.background_resume_capable
    assert capability['binding'] == models._generation_authority_binding()
    assert models._completion_fn is loader and models._responses is subscription
    assert models.public() == public and models.snapshot('generation') == snapshot


@pytest.mark.parametrize('native_kind', ['undeclared', 'different-base', 'function'])
def test_custom_transport_never_gains_capability_from_official_url(tmp_path, native_kind):
    native = (ResponsesCompletion(api_base='https://api.openai.com/v1') if native_kind == 'undeclared'
        else ResponsesCompletion(api_base='https://different.example/v1',
            capabilities=ModelCapabilities(background_resume=True)) if native_kind == 'different-base'
        else lambda **_request: None)
    models = configured(tmp_path, native=native)
    assert models.provider_store_capability()['available'] is False
    assert models.provider_store_adapter() is None


def test_current_reader_blocks_subscription_selection_without_reading_token(tmp_path):
    models = configured(tmp_path)
    with models.records.begin() as tx:
        tx.put('v2_subscription_selection', 'default', {'model': 'synthetic-subscription',
            'mode_revision': 0, 'account_revision': 0}, expected_revision=0)
        assert models.provider_store_capability(reader=tx)['available'] is False
        assert models.provider_store_adapter(reader=tx) is None
        tx.rollback()
    assert models.provider_store_capability()['available'] is True


def test_explicit_local_responses_capability_preserves_off_wire(tmp_path):
    provider = BackgroundProvider('full')
    client = httpx.Client()
    try:
        native = ResponsesCompletion(client=client, api_base=provider.base,
            capabilities=ModelCapabilities(background_resume=True))
        models = configured(tmp_path, base=provider.base, native=native)
        assert models.provider_store_capability()['available'] is False  # local-mode default
        api_mode(models)
        assert models.provider_store_capability()['available'] is True
        assert models.provider_store_adapter() is native
        gateway = models._generation_gateway(models.snapshot('generation'))
        result, usage = gateway.stream_structured_with_usage(
            [{'role': 'user', 'content': 'synthetic'}], response_model=Answer,
            on_delta=lambda _text: None, validate_current=lambda: None, timeout=20)
        assert result.answer == 'hello' and usage['total_tokens'] == 6
        assert provider.calls == [('POST', '/v1/responses')]
        assert 'store' not in provider.bodies[0] and 'background' not in provider.bodies[0]
        assert not client.is_closed
    finally:
        client.close()
        provider.close()
