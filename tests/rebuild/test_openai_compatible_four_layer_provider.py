from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from core.product_core import (
    OpenAICompatibleFourLayerJsonProvider,
    OpenAICompatibleFourLayerProviderError,
    OpenAICompatibleFourLayerProviderSettings,
)


def authorization_headers(value: str = "local-test-key"):
    def provide(url: str) -> Mapping[str, str]:
        assert url == "https://api.deepseek.com/chat/completions"
        return {"Authorization": f"Bearer {value}"}
    return provide


class RecordingPost:
    def __init__(self, *, content: Mapping[str, object] | None = None) -> None:
        self.url = ""
        self.headers: Mapping[str, str] = {}
        self.payload: Mapping[str, object] = {}
        self.timeout_seconds = 0.0
        self.content = content or {
            "candidates": [],
            "insufficient_evidence": ["测试样本没有足够证据。"],
            "provider_boundary": {"provider_must_not": ["publish_memory"]},
        }

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        self.url = url
        self.headers = dict(headers)
        self.payload = dict(payload)
        self.timeout_seconds = timeout_seconds
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(self.content, ensure_ascii=False),
                    }
                }
            ]
        }


def allow_egress(purpose: str, categories: tuple[str, ...], payload_bytes: int):
    assert purpose == "memory_candidate"
    assert categories == ("instructions", "source_excerpt")
    assert payload_bytes > 0
    return lambda status, error_code=None: None


def test_openai_compatible_provider_requests_json_without_payload_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")
    post = RecordingPost()
    provider = OpenAICompatibleFourLayerJsonProvider(
        authorization_header_provider=authorization_headers(),
        http_post_json=post, egress_guard=allow_egress,
    )

    result = provider.complete_json(
        system_prompt="Return four-layer JSON.",
        user_payload={"source_id": "source-alpha", "source_refs": []},
    )
    user_message = post.payload["messages"][1]

    assert provider.provider_name == "deepseek"
    assert post.url == "https://api.deepseek.com/chat/completions"
    assert post.headers["Authorization"] == "Bearer local-test-key"
    assert post.payload["model"] == "deepseek-chat"
    assert post.payload["response_format"] == {"type": "json_object"}
    assert isinstance(user_message, Mapping)
    assert "local-test-key" not in str(user_message["content"])
    assert result["insufficient_evidence"] == ["测试样本没有足够证据。"]
    assert "local-test-key" not in str(result)


def test_openai_compatible_provider_uses_wire_boundary_authorization_callback() -> None:
    post = RecordingPost()
    provider = OpenAICompatibleFourLayerJsonProvider(
        OpenAICompatibleFourLayerProviderSettings(
            provider_name="deepseek",
            endpoint_url="https://api.deepseek.com/chat/completions",
            model="deepseek-chat",
        ),
        authorization_header_provider=authorization_headers("local-secret-reader-key"),
        http_post_json=post,
        egress_guard=allow_egress,
    )

    provider.complete_json(system_prompt="Return JSON.", user_payload={"source_refs": []})

    assert post.headers["Authorization"] == "Bearer local-secret-reader-key"


def test_openai_compatible_provider_accepts_json_code_fence_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")

    def fenced_post(*_args, **_kwargs) -> Mapping[str, object]:
        return {"choices": [{"message": {"content": "```json\n{\"status\": \"ok\"}\n```"}}]}

    provider = OpenAICompatibleFourLayerJsonProvider(
        authorization_header_provider=authorization_headers(),
        http_post_json=fenced_post, egress_guard=allow_egress,
    )
    assert provider.complete_json(system_prompt="Return JSON.", user_payload={"source_refs": []}) == {"status": "ok"}


def test_openai_compatible_provider_does_not_extract_json_from_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")

    def prose_post(*_args, **_kwargs) -> Mapping[str, object]:
        return {"choices": [{"message": {"content": "Here is JSON: {\"status\": \"ok\"}"}}]}

    provider = OpenAICompatibleFourLayerJsonProvider(
        authorization_header_provider=authorization_headers(),
        http_post_json=prose_post, egress_guard=allow_egress,
    )
    with pytest.raises(OpenAICompatibleFourLayerProviderError, match="invalid JSON"):
        provider.complete_json(system_prompt="Return JSON.", user_payload={"source_refs": []})


def test_openai_compatible_provider_errors_do_not_echo_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")

    def invalid_post(
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        assert headers["Authorization"] == "Bearer local-test-key"
        return {"choices": [{"message": {"content": "not-json"}}]}

    provider = OpenAICompatibleFourLayerJsonProvider(
        authorization_header_provider=authorization_headers(),
        http_post_json=invalid_post, egress_guard=allow_egress,
    )

    with pytest.raises(OpenAICompatibleFourLayerProviderError) as error:
        provider.complete_json(system_prompt="Return JSON.", user_payload={"source_refs": []})

    assert "local-test-key" not in str(error.value)


def test_openai_compatible_provider_reports_only_safe_shape_for_missing_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")

    def empty_post(*_args, **_kwargs) -> Mapping[str, object]:
        return {
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "content": None, "reasoning_content": "private model reasoning"},
            }],
        }

    provider = OpenAICompatibleFourLayerJsonProvider(
        authorization_header_provider=authorization_headers(),
        http_post_json=empty_post, egress_guard=allow_egress,
    )
    with pytest.raises(OpenAICompatibleFourLayerProviderError) as error:
        provider.complete_json(system_prompt="Return JSON.", user_payload={"source_refs": []})

    text = str(error.value)
    assert "content_type=NoneType" in text
    assert "finish_reason=length" in text
    assert "message_keys=content,reasoning_content,role" in text
    assert "private model reasoning" not in text
    assert "local-test-key" not in text


def test_openai_compatible_provider_denies_before_network_without_egress_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-read")
    post = RecordingPost()
    provider = OpenAICompatibleFourLayerJsonProvider(http_post_json=post)

    with pytest.raises(OpenAICompatibleFourLayerProviderError, match="egress policy"):
        provider.complete_json(system_prompt="Return JSON.", user_payload={"source_refs": []})

    assert post.url == ""

def test_openai_compatible_provider_checks_injector_after_egress_authorization() -> None:
    guard_called = False

    def guard(purpose: str, categories: tuple[str, ...], payload_bytes: int):
        nonlocal guard_called
        guard_called = True
        return allow_egress(purpose, categories, payload_bytes)

    provider = OpenAICompatibleFourLayerJsonProvider(
        http_post_json=RecordingPost(),
        egress_guard=guard,
    )

    with pytest.raises(OpenAICompatibleFourLayerProviderError, match="injection is not configured"):
        provider.complete_json(system_prompt="Return JSON.", user_payload={"source_refs": []})

    assert guard_called is True


def test_openai_compatible_provider_still_denies_valid_key_when_egress_guard_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")
    post = RecordingPost()

    def deny_egress(purpose: str, categories: tuple[str, ...], payload_bytes: int):
        assert purpose == "memory_candidate"
        assert categories == ("instructions", "source_excerpt")
        assert payload_bytes > 0
        raise RuntimeError("consent_required")

    provider = OpenAICompatibleFourLayerJsonProvider(
        authorization_header_provider=authorization_headers(),
        http_post_json=post, egress_guard=deny_egress,
    )

    with pytest.raises(RuntimeError, match="consent_required"):
        provider.complete_json(system_prompt="Return JSON.", user_payload={"source_refs": []})

    assert post.url == ""
