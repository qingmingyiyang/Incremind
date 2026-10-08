from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import companion_provider_runtime, model_runtime
from core.model_gateway import ModelResult


class _Secrets:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.calls: list[str] = []

    def get(self, key: str) -> str | None:
        self.calls.append(key)
        return self.value


def test_legacy_companion_runtime_stays_local_without_reading_authority(tmp_path) -> None:
    secrets = _Secrets("must-not-be-read")
    provider, capabilities, consented, enabled = companion_provider_runtime.resolve_companion_provider_runtime(
        SimpleNamespace(root_dir=tmp_path, secret_store=secrets),
    )

    assert provider is None
    assert capabilities == ()
    assert consented is False
    assert enabled is False
    assert secrets.calls == []


def test_legacy_companion_runtime_cannot_construct_remote_gateway(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_runtime, "LiteLLMCompletionGateway", lambda **_kwargs: pytest.fail("direct gateway constructed"))
    secrets = _Secrets("secret-value")

    result = companion_provider_runtime.resolve_companion_provider_runtime(
        SimpleNamespace(root_dir=tmp_path, secret_store=secrets),
    )
    assert result == (None, (), False, False)
    assert secrets.calls == []


def test_legacy_companion_runtime_does_not_expose_consent_or_enabled_state(tmp_path) -> None:
    result = companion_provider_runtime.resolve_companion_provider_runtime(
        SimpleNamespace(root_dir=tmp_path, secret_store=_Secrets(None)),
    )
    assert result == (None, (), False, False)


@pytest.mark.parametrize(
    "route_key",
    ["companion.chat", "companion.diary", "companion.event", "companion.ambient", "companion.vision"],
)
def test_model_runtime_factory_preserves_the_exact_provider_projection(
    monkeypatch: pytest.MonkeyPatch, route_key: str,
) -> None:
    provider = SimpleNamespace(model_name="selected-model")
    calls: list[tuple[object, str]] = []
    container = object()
    monkeypatch.setattr(
        companion_provider_runtime,
        "resolve_companion_provider_runtime",
        lambda received, received_route: (calls.append((received, received_route)) or (provider, ("text_generation",), True, False)),
    )

    runtime = companion_provider_runtime.build_companion_model_runtime(container, route_key)

    assert calls == [(container, route_key)]
    assert runtime.model_name == "selected-model"
    assert runtime.router._provider is provider
    assert runtime.router._capabilities == frozenset({"text_generation"})
    assert runtime.router._egress_consented is True
    assert runtime.router._enabled[route_key] is False


def test_model_runtime_factory_names_the_local_fallback_without_a_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        companion_provider_runtime,
        "resolve_companion_provider_runtime",
        lambda *_args: (None, (), False, True),
    )
    runtime = companion_provider_runtime.build_companion_model_runtime(object(), "companion.chat")
    assert runtime.model_name == "local-template-v1"
    assert runtime.router._provider is None
    assert runtime.router._enabled["companion.chat"] is True


def test_litellm_provider_name_maps_only_the_settings_compatibility_alias() -> None:
    assert companion_provider_runtime._litellm_provider_name("custom_openai") == "openai"
    assert companion_provider_runtime._litellm_provider_name("OpenAI") == "openai"
    assert companion_provider_runtime._litellm_provider_name("azure") == "azure"


def test_legacy_vision_runtime_is_unavailable_without_turn_snapshot(tmp_path) -> None:
    _provider, capabilities, consented, enabled = companion_provider_runtime.resolve_companion_provider_runtime(
        SimpleNamespace(root_dir=tmp_path, secret_store=_Secrets("secret")), "companion.vision",
    )
    assert capabilities == () and consented is False and enabled is False


def test_provider_adapter_rejects_malformed_messages() -> None:
    provider = companion_provider_runtime.CompanionLiteLLMProvider(_Gateway(lambda *_a, **_k: "bad"))
    with pytest.raises(RuntimeError, match="messages"):
        provider.generate({"messages": [{"role": "user"}, "not-a-message"]})


def test_provider_adapter_extracts_affect_and_drops_arbitrary_delta() -> None:
    captured = {}

    def complete(messages, **_kwargs):
        captured["messages"] = messages
        return '{"text":"收到啦","affect":"positive","mood_delta":999}'

    result = companion_provider_runtime.CompanionLiteLLMProvider(_Gateway(complete)).generate({
        "messages": [{"role": "system", "content": "safe"}, {"role": "user", "content": "hello"}],
        "max_output_chars": 4000,
        "timeout_ms": 60000,
    })
    assert result["text"] == "收到啦" and result["affect"] == "positive" and "mood_delta" not in result
    assert captured["messages"][-2]["role"] == "system" and captured["messages"][-1]["role"] == "user"


def test_provider_adapter_builds_read_only_vision_payload() -> None:
    captured = {}

    def complete(messages, **_kwargs):
        captured["messages"] = messages
        return "我看到的是测试画面。"

    result = companion_provider_runtime.CompanionLiteLLMProvider(_Gateway(complete)).generate({
        "route_key": "companion.vision",
        "messages": [{"role": "system", "content": "用户自定义角色风格"}, {"role": "user", "content": "结构化问题"}],
        "image_payload": {"media_type": "image/jpeg", "bytes": b"\xff\xd8\xfffixture"},
        "max_output_chars": 4000,
        "timeout_ms": 90000,
    })
    assert captured["messages"][0]["content"] == "用户自定义角色风格"
    assert captured["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert result["text"] == "我看到的是测试画面。"


def test_provider_adapter_rejects_an_unknown_route_before_egress() -> None:
    calls = []
    provider = companion_provider_runtime.CompanionLiteLLMProvider(_Gateway(lambda *_args, **_kwargs: calls.append(True)))
    with pytest.raises(RuntimeError, match="route is invalid"):
        provider.generate({
            "route_key": "companion.unknown",
            "messages": [{"role": "user", "content": "payload"}],
            "max_output_chars": 100,
            "timeout_ms": 1000,
        })
    assert calls == []


class _Gateway:
    def __init__(self, completion) -> None:
        self._completion = completion

    def invoke(self, request):
        parameters = dict(request.parameters)
        messages = parameters.pop("messages")
        output = self._completion(messages, **parameters)
        return ModelResult(output, "test", "test-model", {})
