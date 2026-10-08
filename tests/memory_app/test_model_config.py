from pathlib import Path

import pytest

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.security.secrets import InMemorySecretStore
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


@pytest.fixture
def config(tmp_path):
    return ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / "models.sqlite3"), tmp_path, InMemorySecretStore())


def settings(**changes):
    return dict(base_url="https://example.com/v1", model="example-model", api_key="test-private-value", allow_remote=True, expected_revision=0, **changes)


def test_keys_are_hidden_and_blank_preserves_then_explicit_clear_removes(config):
    saved = config.update("generation", settings())
    assert saved["has_api_key"] and saved["configured"]
    assert "test-private-value" not in str(config.public())
    assert "secret_ref" not in str(config.public())
    assert "test-private-value" not in str(config.records.list("recognition_model_config"))
    updated = config.update("generation", {"expected_revision": 1, "api_key": "", "model": "another-model"})
    assert updated["has_api_key"] and updated["revision"] == 2
    assert config.snapshot("generation")["api_key"] == "test-private-value"
    config.update("generation", {"expected_revision": 2, "clear_api_key": True})
    assert not config.public()["generation"]["configured"]
    with pytest.raises(ModelConfigurationError):
        config.snapshot("generation")


def test_stale_save_preserves_current_key_and_configuration(config):
    config.update("generation", settings())
    with pytest.raises(SQLiteUnitOfWorkConflict):
        config.update("generation", {"expected_revision": 0, "api_key": "replacement", "model": "wrong"})
    assert config.snapshot("generation")["api_key"] == "test-private-value"
    assert config.public()["generation"]["model"] == "example-model"


@pytest.mark.parametrize("purpose", ["generation", "embedding", "rerank"])
def test_disable_remote_preserves_route_key_and_blocks_wire(config, purpose, monkeypatch):
    from backend.memory_app.retrieval_models import configured_adapter
    from backend.recognition_retrieval import RecognitionRetrievalError

    saved = config.update(purpose, settings(enabled=True))
    old_ref = config.records.read("recognition_model_config", purpose).payload["secret_ref"]
    disabled = config.update(purpose, {"allow_remote": False, "api_key": "",
                                      "expected_revision": saved["revision"]})
    assert disabled["base_url"] == saved["base_url"]
    assert disabled["model"] == saved["model"]
    assert disabled["configured"] is True and disabled["has_api_key"] is True
    assert disabled["enabled"] is True and disabled["allow_remote"] is False
    assert disabled["revision"] == saved["revision"] + 1
    assert config.records.read("recognition_model_config", purpose).payload["secret_ref"] == old_ref
    assert config.snapshot(purpose)["api_key"] == "test-private-value"
    assert "test-private-value" not in str(config.public())
    calls = []

    def unexpected_wire(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("disabled remote configuration reached the wire")

    if purpose == "generation":
        config._completion_fn = unexpected_wire
        with pytest.raises(ModelConfigurationError, match="model_egress_remote_not_consented"):
            config.complete([{"role": "user", "content": "test"}])
    else:
        monkeypatch.setattr("backend.memory_app.retrieval_models.httpx.Client", unexpected_wire)
        adapter = configured_adapter(config, purpose)
        with pytest.raises(RecognitionRetrievalError, match="configured_model_egress_remote_not_consented"):
            if purpose == "embedding":
                adapter.embed(["test"])
            else:
                adapter.rerank(query="test", candidates=[{"id": "test", "content": "test"}])
    assert calls == []


@pytest.mark.parametrize("changes", [
    {"base_url": "http://example.com/v1"},
    {"base_url": "https://user:pass@example.com/v1"},
    {"base_url": "https://example.com/v1?key=private"},
    {"expected_revision": True}, {"expected_revision": -1},
])
def test_invalid_or_unapproved_configuration_is_rejected(config, changes):
    data = settings()
    data.update(changes)
    with pytest.raises(ModelConfigurationError):
        config.update("generation", data)
    assert config.public()["generation"]["revision"] == 0


def test_missing_revision_does_not_silently_overwrite(config):
    with pytest.raises(ModelConfigurationError):
        config.update("generation", {"model": "x"})


def test_provider_error_never_reveals_credentials(config, monkeypatch):
    config.update("generation", settings())
    class BrokenGateway:
        def __init__(self, **kwargs):
            pass
        def complete_text_with_usage(self, *args, **kwargs):
            raise RuntimeError("provider response test-private-value")
    monkeypatch.setattr("backend.memory_app.model_config.LiteLLMCompletionGateway", BrokenGateway)
    with pytest.raises(ModelConfigurationError) as failure:
        config.complete([{"role": "user", "content": "hello"}])
    assert "test-private-value" not in str(failure.value)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("max_tokens", [512, 7000])
def test_generation_metadata_uses_each_gateway_wire_budget(config, streaming, max_tokens):
    from backend.memory_app.structured_generation import AskOutput
    from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway, _estimate_input_tokens
    requests = []
    raw = '{"answer":"test answer","citations":[1]}'

    def completion(**request):
        requests.append(request)
        if request.get("stream"):
            return iter([
                {"choices": [{"delta": {"content": raw}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 4, "completion_tokens": 3}},
            ])
        return {"choices": [{"message": {"content": raw}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3}}

    config._completion_fn = completion
    config._gateway_factory = lambda **kwargs: LiteLLMCompletionGateway(
        **{**kwargs, "context_window_tokens": 24000})
    config.update("generation", settings())
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "question"}]
    if streaming:
        _, metadata = config.complete_stream(messages, response_model=AskOutput,
            max_tokens=max_tokens, on_delta=lambda _: None)
    else:
        _, metadata = config.complete_structured(messages, response_model=AskOutput, max_tokens=max_tokens)
    assert metadata["context_budget"] == {"window": 24000, "reserve": max_tokens,
        "estimated_input_tokens": _estimate_input_tokens(requests[-1]["messages"])}
    assert "test-private-value" not in str(metadata)
    assert metadata["usage"]["input_tokens"] == 4


def test_video_chunk_call_can_use_inherited_remote_timeout(config):
    config.update("generation", settings())
    observed = []

    class Gateway:
        def complete_text_with_usage(self, messages, *, max_tokens, timeout):
            observed.append((max_tokens, timeout))
            return "{}", {}, None

    config._gateway_factory = lambda **_kwargs: Gateway()
    config.complete([{"role": "user", "content": "short sample"}], max_tokens=7000, timeout_seconds=90)
    assert observed == [(7000, 90)]


def test_explicit_local_chunk_timeout_is_not_replaced_by_default(config, tmp_path):
    config.update("generation", settings())
    model_file = tmp_path / "data/models/qwen2.5-1.5b-instruct/model.safetensors"
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"installed-model-test")
    config.update_generation_mode(
        mode="local", local_enabled=True,
        local_base_url="http://127.0.0.1:8001/local-model/v1", expected_revision=0,
    )
    observed = []

    class Gateway:
        def complete_text_with_usage(self, messages, *, max_tokens, timeout):
            observed.append(timeout)
            return "{}", {}, None

    config._gateway_factory = lambda **_kwargs: Gateway()
    config.complete([{"role": "user", "content": "bounded segment"}], timeout_seconds=35)
    config.complete([{"role": "user", "content": "default segment"}])
    assert observed == [35, 180]


def test_generation_mode_requires_explicit_local_enable_and_preserves_api_key(config, tmp_path):
    config.update("generation", settings())
    assert config.generation_mode()["mode"] == "api"
    with pytest.raises(ModelConfigurationError, match="enable_local_model_before_selection"):
        config.update_generation_mode(
            mode="local", local_enabled=False,
            local_base_url="http://127.0.0.1:8001/local-model/v1", expected_revision=0,
        )
    model_file = tmp_path / "data/models/qwen2.5-1.5b-instruct/model.safetensors"
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"installed-model-test")
    local = config.update_generation_mode(
        mode="local", local_enabled=True,
        local_base_url="http://127.0.0.1:8001/local-model/v1", expected_revision=0,
    )
    assert local["mode"] == "local" and local["local_enabled"] is True
    assert local["api_configured"] is True
    assert config.local_generation_allowed() is True
    assert config.public()["generation"]["configured"] is True
    assert config.public()["generation"]["has_api_key"] is False
    assert config.snapshot("generation")["api_key"] == "local-model"
    with pytest.raises(ModelConfigurationError, match="select_api_before_editing_generation"):
        config.update("generation", {"expected_revision": 2, "model": "wrong"})
    api = config.update_generation_mode(
        mode="api", local_enabled=False,
        local_base_url="http://127.0.0.1:8001/local-model/v1", expected_revision=1,
    )
    assert api["mode"] == "api" and api["local_enabled"] is False
    assert config.local_generation_allowed() is False
    assert config.snapshot("generation")["api_key"] == "test-private-value"


def test_generation_mode_rejects_wrong_endpoint_and_stale_revision(config, tmp_path):
    config.update("generation", settings())
    model_file = tmp_path / "data/models/qwen2.5-1.5b-instruct/model.safetensors"
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"installed-model-test")
    with pytest.raises(ModelConfigurationError, match="invalid_local_model_endpoint"):
        config.update_generation_mode(
            mode="local", local_enabled=True,
            local_base_url="https://example.com/local-model/v1", expected_revision=0,
        )
    config.update_generation_mode(
        mode="api", local_enabled=True,
        local_base_url="http://127.0.0.1:8001/local-model/v1", expected_revision=0,
    )
    with pytest.raises(ModelConfigurationError, match="generation_mode_revision_conflict"):
        config.update_generation_mode(
            mode="local", local_enabled=True,
            local_base_url="http://127.0.0.1:8001/local-model/v1", expected_revision=0,
        )
    with pytest.raises(ModelConfigurationError, match="select_local_model_in_generation_mode"):
        config.update("generation", {
            "expected_revision": 1, "base_url": "http://127.0.0.1:8001/local-model/v1",
            "allow_remote": False,
        })
