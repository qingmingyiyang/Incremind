import asyncio

import pytest
from pydantic import BaseModel

from backend.shared.llm.model_capabilities import ModelCapabilities, resolve_model_capabilities
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway


class Answer(BaseModel):
    answer: str


class Lease:
    def finish(self, status, *, error_code=None):
        self.status = status


def gateway(completion, *, base_url="https://example.invalid/v1", **kwargs):
    return LiteLLMCompletionGateway(provider="openai", model="deepseek-flash",
        base_url=base_url, api_key="test-value", completion_fn=completion,
        acompletion_fn=kwargs.pop("acompletion_fn", None),
        egress_guard=lambda *_args: Lease(), **kwargs)


@pytest.mark.parametrize("url", ["https://api.deepseek.com", "https://api.deepseek.com/v1", "https://api.deepseek.com/beta"])
def test_deepseek_chat_profile_uses_only_documented_structured_modes(url):
    profile = resolve_model_capabilities("openai", "deepseek-flash", url)
    assert profile.structured_modes == ("json_object", "prompt")
    assert profile.developer_role is False
    assert "max" in profile.reasoning_efforts


@pytest.mark.parametrize("url", ["https://api.deepseek.com.evil.invalid/v1", "https://proxy.invalid/deepseek", "http://127.0.0.1:8001/v1"])
def test_unknown_endpoints_keep_existing_compatibility_probe(url):
    assert resolve_model_capabilities("openai", "deepseek-flash", url) == ModelCapabilities()


def test_deepseek_schema_is_not_sent_and_response_is_still_validated():
    calls = []
    def completion(**request):
        calls.append(request)
        return {"choices": [{"message": {"content": '{"answer":"ok"}'}}]}
    result = gateway(completion, base_url="https://api.deepseek.com").complete_structured(
        [{"role": "user", "content": "reply as JSON"}], response_model=Answer)
    assert result.answer == "ok"
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_retries"] == 0


def test_explicit_profile_adapts_parameters_without_mutating_messages():
    calls = []
    def completion(**request):
        calls.append(request)
        return {"choices": [{"message": {"content": "ok"}}]}
    profile = ModelCapabilities(developer_role=False, max_tokens_field="max_completion_tokens")
    messages = [{"role": "developer", "content": "instructions"}]
    gateway(completion, capabilities=profile).complete_text(messages, max_tokens=12)
    assert calls[0]["messages"] == [{"role": "system", "content": "instructions"}]
    assert messages[0]["role"] == "developer"
    assert calls[0]["max_completion_tokens"] == 12
    assert "max_tokens" not in calls[0]


@pytest.mark.parametrize("kind", ["text", "metadata", "async"])
def test_stream_usage_can_be_disabled_by_profile(kind):
    calls = []
    chunks = [{"choices": [{"delta": {"content": "ok"}}]}]
    def completion(**request):
        calls.append(request)
        return iter(chunks)
    async def acompletion(**request):
        calls.append(request)
        async def stream():
            for chunk in chunks:
                yield chunk
        return stream()
    model = gateway(completion, capabilities=ModelCapabilities(stream_usage=False), acompletion_fn=acompletion)
    if kind == "async":
        async def consume():
            return [part async for part in model.astream_text([{"role": "user", "content": "ping"}])]
        assert asyncio.run(consume()) == ["ok"]
    elif kind == "metadata":
        assert [part.delta for part in model.stream_text_with_metadata([{"role": "user", "content": "ping"}])] == ["ok"]
    else:
        assert list(model.stream_text([{"role": "user", "content": "ping"}])) == ["ok"]
    assert "stream_options" not in calls[0]


def test_deepseek_accepts_max_effort_and_unknown_profile_retains_old_levels():
    calls = []
    def completion(**request):
        calls.append(request)
        return {"choices": [{"message": {"content": "ok"}}]}
    gateway(completion, base_url="https://api.deepseek.com", reasoning_effort="max").complete_text(
        [{"role": "user", "content": "ping"}])
    assert calls[0]["reasoning_effort"] == "max"
    with pytest.raises(RuntimeError, match="unsupported reasoning_effort"):
        gateway(completion, reasoning_effort="max")


def test_profile_rejects_invalid_declarations():
    with pytest.raises(ValueError):
        ModelCapabilities(max_tokens_field="api_key")
    with pytest.raises(ValueError):
        ModelCapabilities(structured_modes=("unknown",))
