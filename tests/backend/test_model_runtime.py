from __future__ import annotations

import pytest

from backend.model_runtime import LiteLLMModelGatewayAdapter, ModelRuntimeError
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
from core.model_gateway import ModelRequest


class _LiteGateway:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[object, dict[str, object]]] = []

    def complete_text(self, messages, **parameters):
        self.calls.append((messages, parameters))
        return self.output


class _ExecutionControl:
    def __init__(self, remaining_timeout_ms: int = 2500) -> None:
        self.remaining_timeout_ms = remaining_timeout_ms
        self.cancel_requested = False
        self.checkpoints = 0

    def checkpoint(self) -> None:
        self.checkpoints += 1
        if self.cancel_requested:
            raise RuntimeError("cancelled")


class _MetadataSink:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.completed: list[dict[str, int]] = []
        self.cache_observations: list[dict[str, int]] = []

    def model_call_started(self, *, provider: str, model: str) -> None:
        self.started.append((provider, model))

    def model_call_completed(self, *, usage) -> None:
        self.completed.append(dict(usage))

    def model_call_failed(self) -> None:
        self.failed = getattr(self, "failed", 0) + 1

    def model_call_cache_observed(self, *, observation) -> None:
        self.cache_observations.append(dict(observation))


def test_model_gateway_rejects_local_only_before_remote_adapter_call() -> None:
    lite = _LiteGateway("must-not-run")
    gateway = LiteLLMModelGatewayAdapter(lite, provider="openai", model="safe-model")
    with pytest.raises(ModelRuntimeError, match="remote_allowed"):
        gateway.invoke(ModelRequest("text", "private", {}, "local_only"))
    assert lite.calls == []


def test_model_gateway_maps_structured_request_and_provider_metadata() -> None:
    lite = _LiteGateway('{"type":"complete","summary":"done"}')
    gateway = LiteLLMModelGatewayAdapter(lite, provider="openai", model="safe-model")
    result = gateway.invoke(ModelRequest(
        "structured",
        "plan",
        {"temperature": 0, "max_tokens": 500, "timeout": 4.0, "response_format": {"type": "json_object"}},
        "remote_allowed",
    ))
    assert result.output == {"type": "complete", "summary": "done"}
    assert result.provider == "openai" and result.model == "safe-model"
    assert lite.calls[0][0] == [{"role": "user", "content": "plan"}]
    assert lite.calls[0][1]["response_format"] == {"type": "json_object"}


def test_model_gateway_rejects_invalid_structured_output() -> None:
    gateway = LiteLLMModelGatewayAdapter(_LiteGateway("not-json"), provider="openai", model="safe-model")
    with pytest.raises(ModelRuntimeError, match="not JSON"):
        gateway.invoke(ModelRequest("structured", "plan", {}, "remote_allowed"))


def test_local_json_parse_failure_keeps_provider_wire_terminal_succeeded() -> None:
    terminals: list[str] = []

    class _Handle:
        def invoke_wire(self, handler):
            return handler()

        def succeeded(self, *, usage, cache_observation) -> None:
            terminals.append("succeeded")

        def failed_transport(self, *, error_code) -> None:
            terminals.append("failed_transport")

        def consumer_cancelled(self) -> None:
            terminals.append("consumer_cancelled")

    class _Sink:
        def begin_model_wire_attempt(self):
            return _Handle()

    class _Lease:
        def finish(self, status, *, error_code=None):
            return None

    lite = LiteLLMCompletionGateway(
        provider="openai", model="safe-model",
        base_url="https://example.invalid/v1", api_key="test-key",
        completion_fn=lambda **_kwargs: {
            "choices": [{"message": {"content": "not-json"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        acompletion_fn=lambda **_kwargs: None,
        egress_guard=lambda _purpose, _categories, _bytes: _Lease(),
    )

    with pytest.raises(ModelRuntimeError, match="not JSON"):
        LiteLLMModelGatewayAdapter(
            lite, provider="openai", model="safe-model",
        ).invoke(ModelRequest(
            "structured", "plan", {}, "remote_allowed",
            wire_attempt_sink=_Sink(),
        ))

    assert terminals == ["succeeded"]


def test_model_gateway_reports_provider_transport_failure_without_content() -> None:
    class _FailingGateway(_LiteGateway):
        def complete_text(self, messages, **parameters):
            raise OSError("provider unavailable")

    sink = _MetadataSink()
    gateway = LiteLLMModelGatewayAdapter(
        _FailingGateway("unused"), provider="openai", model="safe-model",
    )

    with pytest.raises(OSError, match="unavailable"):
        gateway.invoke(ModelRequest(
            "text", "private input", {}, "remote_allowed", metadata_sink=sink,
        ))

    assert sink.started == [("openai", "safe-model")]
    assert sink.completed == []
    assert sink.failed == 1


def test_model_gateway_preserves_provider_failure_when_failure_telemetry_fails() -> None:
    class _FailingGateway(_LiteGateway):
        def complete_text(self, messages, **parameters):
            raise OSError("provider unavailable")

    class _FailingTelemetrySink(_MetadataSink):
        def model_call_failed(self) -> None:
            raise RuntimeError("failure telemetry unavailable")

    gateway = LiteLLMModelGatewayAdapter(
        _FailingGateway("unused"), provider="openai", model="safe-model",
    )

    with pytest.raises(OSError, match="provider unavailable"):
        gateway.invoke(ModelRequest(
            "text", "private input", {}, "remote_allowed",
            metadata_sink=_FailingTelemetrySink(),
        ))


def test_model_gateway_maps_invocation_local_vision_payload_to_multimodal_message() -> None:
    lite = _LiteGateway("画面分析结果")
    gateway = LiteLLMModelGatewayAdapter(lite, provider="openai", model="vision-model")

    result = gateway.invoke(ModelRequest(
        "vision",
        "不要持久化像素",
        {
            "messages": [
                {"role": "system", "content": "只依据画面"},
                {"role": "user", "content": "画面里有什么"},
            ],
            "image_payload": {"media_type": "image/jpeg", "pixels": b"jpeg-bytes"},
        },
        "remote_allowed",
    ))

    assert result.output == "画面分析结果"
    content = lite.calls[0][0][-1]["content"]
    assert content[0] == {"type": "text", "text": "画面里有什么"}
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert b"jpeg-bytes" not in repr(result).encode()


def test_model_gateway_applies_remaining_execution_deadline_and_checkpoints() -> None:
    lite = _LiteGateway("done")
    control = _ExecutionControl(remaining_timeout_ms=2500)
    gateway = LiteLLMModelGatewayAdapter(lite, provider="openai", model="safe-model")

    result = gateway.invoke(ModelRequest(
        "text",
        "bounded",
        {"timeout": 8.0},
        "remote_allowed",
        execution_control=control,
    ))

    assert result.output == "done"
    assert lite.calls[0][1]["timeout"] == 2.5
    assert control.checkpoints == 2


def test_model_gateway_reports_only_provider_model_and_available_usage_metadata() -> None:
    sink = _MetadataSink()
    gateway = LiteLLMModelGatewayAdapter(
        _LiteGateway("done"), provider="openai", model="safe-model",
    )

    gateway.invoke(ModelRequest(
        "text", "private input", {}, "remote_allowed", metadata_sink=sink,
    ))

    assert sink.started == [("openai", "safe-model")]
    assert sink.completed == [{}]


def test_model_gateway_forwards_only_sanitized_provider_cache_observation() -> None:
    class _UsageGateway(_LiteGateway):
        def complete_text_with_usage(self, messages, **parameters):
            self.calls.append((messages, parameters))
            return (
                "done",
                {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24},
                {"cache_read_input_tokens": 17, "cache_miss_input_tokens": 3},
            )

    sink = _MetadataSink()
    result = LiteLLMModelGatewayAdapter(
        _UsageGateway("unused"), provider="deepseek", model="safe-model",
    ).invoke(ModelRequest(
        "text", "private input", {}, "remote_allowed", metadata_sink=sink,
    ))

    assert result.usage == {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}
    assert sink.completed == [dict(result.usage)]
    assert sink.cache_observations == [{
        "cache_read_input_tokens": 17,
        "cache_miss_input_tokens": 3,
    }]


def test_model_gateway_forwards_runtime_owned_wire_attempt_sink() -> None:
    class _UsageGateway(_LiteGateway):
        def complete_text_with_usage(self, messages, **parameters):
            self.calls.append((messages, parameters))
            return "done", {}, None

    class _AttemptSink:
        def begin_model_wire_attempt(self):
            raise AssertionError("fake transport must only receive the sink")

    lite = _UsageGateway("unused")
    attempt_sink = _AttemptSink()

    result = LiteLLMModelGatewayAdapter(
        lite, provider="openai", model="safe-model",
    ).invoke(ModelRequest(
        "text", "private input", {}, "remote_allowed",
        wire_attempt_sink=attempt_sink,
    ))

    assert result.output == "done"
    assert lite.calls[0][1]["wire_attempt_sink"] is attempt_sink


def test_model_gateway_keeps_success_when_post_egress_telemetry_fails() -> None:
    class _UsageGateway(_LiteGateway):
        def complete_text_with_usage(self, messages, **parameters):
            self.calls.append((messages, parameters))
            return (
                "provider-success",
                {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
                {"cache_read_input_tokens": 5},
            )

    class _FailingTelemetrySink(_MetadataSink):
        def model_call_cache_observed(self, *, observation) -> None:
            raise OSError("cache telemetry unavailable")

        def model_call_completed(self, *, usage) -> None:
            raise OSError("completion telemetry unavailable")

    result = LiteLLMModelGatewayAdapter(
        _UsageGateway("unused"), provider="openai", model="safe-model",
    ).invoke(ModelRequest(
        "text", "private input", {}, "remote_allowed",
        metadata_sink=_FailingTelemetrySink(),
    ))

    assert result.output == "provider-success"
    assert result.usage == {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10}


def test_model_gateway_postflight_checkpoint_blocks_cancelled_result() -> None:
    control = _ExecutionControl()
    sink = _MetadataSink()

    class _CancellingGateway(_LiteGateway):
        def complete_text(self, messages, **parameters):
            value = super().complete_text(messages, **parameters)
            control.cancel_requested = True
            return value

    lite = _CancellingGateway("must-not-be-consumed")
    gateway = LiteLLMModelGatewayAdapter(lite, provider="openai", model="safe-model")

    with pytest.raises(RuntimeError, match="cancelled"):
        gateway.invoke(ModelRequest(
            "text", "bounded", {}, "remote_allowed", execution_control=control,
            metadata_sink=sink,
        ))
    assert len(lite.calls) == 1
    assert control.checkpoints == 2
    assert sink.started == [("openai", "safe-model")]
    assert sink.completed == []
