from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.app import create_app
from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore


PRIVATE_KEY = "test-private-value"


def _settings(**changes):
    return {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "api_key": PRIVATE_KEY,
        "allow_remote": True,
        "expected_revision": 0,
        **changes,
    }


def _config(tmp_path: Path, gateway_factory, completion_fn=None):
    return ModelConfiguration(
        SQLiteStructuredRecordStore(tmp_path / "models.sqlite3"),
        tmp_path,
        InMemorySecretStore(),
        gateway_factory=gateway_factory,
        completion_fn=completion_fn,
    )


def test_consented_remote_snapshot_wires_real_gateway_with_fake_completion(tmp_path):
    calls = []

    def completion(**request):
        calls.append(request)
        return {"choices": [{"message": {"content": "accepted"}}], "usage": {"total_tokens": 3}}

    def gateway_factory(**kwargs):
        return LiteLLMCompletionGateway(**kwargs)

    config = _config(tmp_path, gateway_factory, completion)
    config.update("generation", _settings())

    text, metadata = config.complete([{"role": "user", "content": "hello"}])

    assert text == "accepted"
    assert metadata["configuration_revision"] == 1
    assert len(calls) == 1
    assert calls[0]["api_base"] == "https://api.deepseek.com/v1"
    assert calls[0]["model"] == "openai/deepseek-flash"


def test_remote_configuration_without_consent_is_denied_before_wire_and_hides_key(tmp_path):
    calls = []

    def completion(**request):
        calls.append(request)
        return {"choices": [{"message": {"content": "unexpected"}}]}

    def gateway_factory(**kwargs):
        return LiteLLMCompletionGateway(**kwargs)

    config = _config(tmp_path, gateway_factory, completion)
    config.secrets.set("manual-secret", PRIVATE_KEY)
    with config.records.begin() as tx:
        tx.put(
            "recognition_model_config",
            "generation",
            {
                "provider": "openai",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-flash",
                "allow_remote": False,
                "enabled": False,
                "secret_ref": "manual-secret",
            },
            expected_revision=0,
        )
        tx.commit()

    with pytest.raises(ModelConfigurationError) as failure:
        config.complete([{"role": "user", "content": "hello"}])

    assert calls == []
    assert PRIVATE_KEY not in str(failure.value)


def test_configuration_change_after_gateway_construction_is_denied_before_wire(tmp_path):
    calls = []
    config = None

    def completion(**request):
        calls.append(request)
        return {"choices": [{"message": {"content": "unexpected"}}]}

    def gateway_factory(**kwargs):
        assert config is not None
        config.update(
            "generation",
            {
                "expected_revision": 1,
                "base_url": "http://127.0.0.1:8317/v1",
                "allow_remote": False,
                "api_key": "",
            },
        )
        return LiteLLMCompletionGateway(**kwargs)

    config = _config(tmp_path, gateway_factory, completion)
    config.update("generation", _settings())

    with pytest.raises(ModelConfigurationError) as failure:
        config.complete([{"role": "user", "content": "hello"}])

    assert calls == []
    assert "model_configuration_changed_before_request" in str(failure.value)
    assert PRIVATE_KEY not in str(failure.value)
