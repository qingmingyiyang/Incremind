from __future__ import annotations

import asyncio
from time import monotonic, sleep
import unittest
from unittest.mock import patch


from pydantic import BaseModel, Field

from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway, clear_structured_mode_cache
class SeriesAnswerPayload(BaseModel):
    answer: str
    citations: list[str] = Field(default_factory=list)
    used_source_types: list[str] = Field(default_factory=list)


class _EgressLease:
    def finish(self, status: str, *, error_code: str | None = None) -> None:
        return None


class _RecordingLease:
    def __init__(self) -> None:
        self.outcomes: list[tuple[str, str | None]] = []

    def finish(self, status: str, *, error_code: str | None = None) -> None:
        self.outcomes.append((status, error_code))


class _RecordingEgress:
    def __init__(self) -> None:
        self.leases: list[_RecordingLease] = []

    def __call__(
        self, _purpose: str, _categories: tuple[str, ...], _payload_bytes: int,
    ) -> _RecordingLease:
        lease = _RecordingLease()
        self.leases.append(lease)
        return lease


class _WireAttemptHandle:
    def __init__(self, records: list[tuple[str, object]]) -> None:
        self.records = records

    def invoke_wire(self, handler):
        self.records.append(("handler", None))
        return handler()

    def succeeded(self, *, usage, cache_observation) -> None:
        self.records.append(("succeeded", (dict(usage), cache_observation)))

    def failed_transport(self, *, error_code: str) -> None:
        self.records.append(("failed_transport", error_code))

    def consumer_cancelled(self) -> None:
        self.records.append(("consumer_cancelled", None))


class _WireAttemptSink:
    def __init__(self, records: list[tuple[str, object]], *, fail_begin: bool = False) -> None:
        self.records = records
        self.fail_begin = fail_begin

    def begin_model_wire_attempt(self) -> _WireAttemptHandle:
        self.records.append(("dispatched", None))
        if self.fail_begin:
            raise RuntimeError("attempt dispatch persistence failed")
        return _WireAttemptHandle(self.records)


def allow_egress(purpose: str, categories: tuple[str, ...], payload_bytes: int) -> _EgressLease:
    assert purpose == "model_completion"
    assert categories == ("instructions", "source_excerpt")
    assert payload_bytes > 0
    return _EgressLease()


class LiteLLMCompletionGatewayStructuredModeTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_structured_mode_cache()

    def test_refuses_before_completion_when_egress_policy_is_missing(self) -> None:
        completion = CapturingCompletion("must-not-run")
        wire_records: list[tuple[str, object]] = []
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
        )

        with self.assertRaisesRegex(RuntimeError, "外发政策未配置"):
            gateway.complete_text(
                [{"role": "user", "content": "ping"}],
                wire_attempt_sink=_WireAttemptSink(wire_records),
            )

        self.assertEqual(completion.messages, [])
        self.assertEqual(wire_records, [])

    def test_token_hard_budget_rejects_before_secret_egress_and_wire(self) -> None:
        completion = CapturingCompletion("must-not-run")
        egress = _RecordingEgress()
        secret_calls: list[str] = []
        wire_records: list[tuple[str, object]] = []
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key=None,
            api_key_provider=lambda: secret_calls.append("materialized") or "test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=egress,
            context_window_tokens=64,
            reserved_output_tokens=16,
        )

        with self.assertRaisesRegex(RuntimeError, "Token 硬预算"):
            gateway.complete_text(
                [{"role": "user", "content": "x" * 500}],
                wire_attempt_sink=_WireAttemptSink(wire_records),
            )

        self.assertEqual(secret_calls, [])
        self.assertEqual(egress.leases, [])
        self.assertEqual(completion.messages, [])
        self.assertEqual(wire_records, [])

    def test_input_budget_snapshot_uses_actual_structured_messages_and_is_a_copy(self) -> None:
        from backend.memory_app.structured_generation import AskOutput
        from backend.shared.llm.litellm_gateway import _estimate_input_tokens
        from backend.shared.llm.model_capabilities import ModelCapabilities
        completion = CapturingCompletion('{"answer":"ok","citations":[1]}')
        gateway = LiteLLMCompletionGateway(provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=completion, acompletion_fn=unused_async_completion,
            egress_guard=allow_egress, context_window_tokens=24000, reserved_output_tokens=2000,
            capabilities=ModelCapabilities(structured_modes=("prompt",)))
        self.assertIsNone(gateway.input_budget_snapshot())
        original = [{"role": "user", "content": "test context"}]
        gateway.complete_structured_with_usage(original, response_model=AskOutput, max_tokens=7000)
        actual = _estimate_input_tokens(completion.messages[-1])
        self.assertGreater(actual, _estimate_input_tokens(original))
        self.assertEqual(gateway.input_budget_snapshot(), {
            "window": 24000, "reserve": 7000, "estimated_input_tokens": actual})
        snapshot = gateway.input_budget_snapshot()
        snapshot["window"] = 1
        self.assertEqual(gateway.input_budget_snapshot()["window"], 24000)
        gateway.complete_text(original)
        self.assertEqual(gateway.input_budget_snapshot()["reserve"], 2000)

    def test_uses_litellm_pydantic_schema_first_for_openai_models(self) -> None:
        completion = CapturingCompletion(
            '{"answer": "ok", "citations": ["e1"], "used_source_types": ["transcript"]}'
        )
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        result = gateway.complete_structured(
            [{"role": "user", "content": "回答问题"}],
            response_model=SeriesAnswerPayload,
        )

        self.assertEqual(result.citations, ["e1"])
        self.assertIs(completion.response_formats[0], SeriesAnswerPayload)
        prompt = "\n".join(str(message["content"]) for message in completion.messages[0])
        self.assertNotIn("JSON Schema", prompt)

    def test_adds_v1_suffix_to_root_base_url_for_requests(self) -> None:
        completion = CapturingCompletion("ok")
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://jiuuij.de5.net",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        result = gateway.complete_text([{"role": "user", "content": "ping"}])

        self.assertEqual(result, "ok")
        self.assertEqual(completion.api_bases, ["https://jiuuij.de5.net/v1"])

    def test_keeps_existing_v1_suffix_for_requests(self) -> None:
        completion = CapturingCompletion("ok")
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://jiuuij.de5.net/v1/",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        gateway.complete_text([{"role": "user", "content": "ping"}])

        self.assertEqual(completion.api_bases, ["https://jiuuij.de5.net/v1"])

    def test_passes_reasoning_effort_to_litellm_requests(self) -> None:
        completion = CapturingCompletion("ok")
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            reasoning_effort="high",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        gateway.complete_text([{"role": "user", "content": "ping"}])

        self.assertEqual(completion.reasoning_efforts, ["high"])

    def test_allows_reasoning_effort_for_openai_compatible_custom_models(self) -> None:
        completion = CapturingCompletion("ok")
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="deepseek-v4-pro",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            reasoning_effort="medium",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        gateway.complete_text([{"role": "user", "content": "ping"}])

        self.assertEqual(completion.allowed_openai_params, [["reasoning_effort"]])

    def test_omits_reasoning_effort_when_disabled(self) -> None:
        completion = CapturingCompletion("ok")
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            reasoning_effort="none",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        gateway.complete_text([{"role": "user", "content": "ping"}])

        self.assertEqual(completion.reasoning_efforts, [None])

    def test_raises_clear_error_when_reasoning_effort_is_not_supported(self) -> None:
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            reasoning_effort="high",
            completion_fn=RejectingReasoningEffortCompletion(),
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        with self.assertRaisesRegex(RuntimeError, "此模型不支持思考强度"):
            gateway.complete_text([{"role": "user", "content": "ping"}])

    def test_uses_litellm_provider_prefix_for_bare_model_names(self) -> None:
        completion = CapturingCompletion("ok")
        gateway = LiteLLMCompletionGateway(
            provider="deepseek",
            model="deepseek-v4-pro",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        gateway.complete_text([{"role": "user", "content": "ping"}])

        self.assertEqual(completion.models, ["deepseek/deepseek-v4-pro"])

    def test_falls_back_to_json_object_when_schema_is_rejected(self) -> None:
        completion = RejectingFirstResponseFormatCompletion(
            SeriesAnswerPayload,
            '{"answer": "ok", "citations": ["e1"], "used_source_types": ["transcript"]}',
        )
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        result = gateway.complete_structured(
            [{"role": "user", "content": "回答问题"}],
            response_model=SeriesAnswerPayload,
        )

        self.assertEqual(result.citations, ["e1"])
        self.assertEqual(completion.response_formats, [SeriesAnswerPayload, {"type": "json_object"}])
        prompt = "\n".join(str(message["content"]) for message in completion.messages[1])
        self.assertIn("只输出一个 JSON 对象", prompt)
        self.assertNotIn("JSON Schema", prompt)

    def test_caches_schema_mode_after_success_for_same_endpoint(self) -> None:
        completion = CapturingCompletion(
            '{"answer": "ok", "citations": ["e1"], "used_source_types": ["transcript"]}'
        )
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        gateway.complete_structured(
            [{"role": "user", "content": "第一次"}],
            response_model=SeriesAnswerPayload,
        )
        gateway.complete_structured(
            [{"role": "user", "content": "第二次"}],
            response_model=SeriesAnswerPayload,
        )

        self.assertEqual(completion.response_formats, [SeriesAnswerPayload, SeriesAnswerPayload])

    def test_cached_schema_mode_falls_back_when_later_schema_is_rejected(self) -> None:
        completion = RejectingBaseModelResponseFormatsCompletion(
            '{"answer": "ok", "citations": ["e1"], "used_source_types": ["transcript"]}'
        )
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        gateway.complete_structured(
            [{"role": "user", "content": "第一次"}],
            response_model=SeriesAnswerPayload,
        )
        result = gateway.complete_structured(
            [{"role": "user", "content": "第二次"}],
            response_model=SeriesAnswerPayload,
        )

        self.assertEqual(result.citations, ["e1"])
        self.assertEqual(
            completion.response_formats,
            [SeriesAnswerPayload, SeriesAnswerPayload, {"type": "json_object"}],
        )

    def test_caches_json_object_mode_after_schema_rejection_for_same_endpoint(self) -> None:
        completion = RejectingFirstResponseFormatCompletion(
            SeriesAnswerPayload,
            '{"answer": "ok", "citations": ["e1"], "used_source_types": ["transcript"]}',
        )
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        gateway.complete_structured(
            [{"role": "user", "content": "第一次"}],
            response_model=SeriesAnswerPayload,
        )
        gateway.complete_structured(
            [{"role": "user", "content": "第二次"}],
            response_model=SeriesAnswerPayload,
        )

        self.assertEqual(
            completion.response_formats,
            [SeriesAnswerPayload, {"type": "json_object"}, {"type": "json_object"}],
        )

    def test_cache_is_scoped_by_api_key(self) -> None:
        first_completion = RejectingFirstResponseFormatCompletion(
            SeriesAnswerPayload,
            '{"answer": "ok", "citations": ["e1"], "used_source_types": ["transcript"]}',
        )
        first_gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="first-key",
            completion_fn=first_completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )
        second_completion = CapturingCompletion(
            '{"answer": "ok", "citations": ["e1"], "used_source_types": ["transcript"]}'
        )
        second_gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="second-key",
            completion_fn=second_completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        first_gateway.complete_structured(
            [{"role": "user", "content": "第一次"}],
            response_model=SeriesAnswerPayload,
        )
        second_gateway.complete_structured(
            [{"role": "user", "content": "第二次"}],
            response_model=SeriesAnswerPayload,
        )

        self.assertEqual(first_completion.response_formats, [SeriesAnswerPayload, {"type": "json_object"}])
        self.assertEqual(second_completion.response_formats, [SeriesAnswerPayload])

    def test_falls_back_to_prompt_schema_when_response_format_is_rejected(self) -> None:
        completion = RejectingResponseFormatsCompletion(
            '{"answer": "ok", "citations": ["e1"], "used_source_types": ["transcript"]}'
        )
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        result = gateway.complete_structured(
            [{"role": "user", "content": "回答问题"}],
            response_model=SeriesAnswerPayload,
        )

        self.assertEqual(result.citations, ["e1"])
        self.assertEqual(completion.response_formats, [SeriesAnswerPayload, {"type": "json_object"}, None])
        prompt = "\n".join(str(message["content"]) for message in completion.messages[2])
        self.assertIn("只输出一个 JSON 对象", prompt)
        self.assertIn("不要输出 Markdown", prompt)
        self.assertIn('"SeriesAnswerPayload"', prompt)
        self.assertIn('"citations"', prompt)

    def test_structured_completion_propagates_bounded_timeout(self) -> None:
        completion = CapturingCompletion(
            '{"answer": "ok", "citations": [], "used_source_types": []}'
        )
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        gateway.complete_structured(
            [{"role": "user", "content": "bounded"}],
            response_model=SeriesAnswerPayload,
            timeout=12.5,
        )

        self.assertEqual(len(completion.timeouts), 1)
        self.assertGreater(completion.timeouts[0], 0)
        self.assertLessEqual(completion.timeouts[0], 12.5)

    def test_timeout_error_is_not_retried_or_changed_to_format_fallback(self) -> None:
        completion = TimingOutCompletion()
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        with self.assertRaisesRegex(TimeoutError, "controlled deadline"):
            gateway.complete_structured(
                [{"role": "user", "content": "slow"}],
                response_model=SeriesAnswerPayload,
                retries=2,
                timeout=7.0,
            )

        self.assertEqual(completion.calls, 1)
        self.assertEqual(len(completion.timeouts), 1)
        self.assertGreater(completion.timeouts[0], 0)
        self.assertLessEqual(completion.timeouts[0], 7.0)

    def test_validation_retries_share_one_total_deadline_budget(self) -> None:
        completion = CapturingCompletion("not-json")
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        with patch(
            "backend.shared.llm.litellm_gateway.monotonic",
            side_effect=[100.0, 100.1, 106.0],
        ):
            with self.assertRaisesRegex(TimeoutError, "统一 deadline"):
                gateway.complete_structured(
                    [{"role": "user", "content": "invalid"}],
                    response_model=SeriesAnswerPayload,
                    retries=2,
                    timeout=5.0,
                )

        self.assertEqual(len(completion.messages), 1)
        self.assertAlmostEqual(completion.timeouts[0], 4.9)

    def test_metadata_stream_propagates_bounded_timeout(self) -> None:
        completion = StreamingCompletion()
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        chunks = list(
            gateway.stream_text_with_metadata(
                [{"role": "user", "content": "stream"}],
                timeout=9.0,
            )
        )

        self.assertEqual([chunk.delta for chunk in chunks], ["ok"])
        self.assertEqual(completion.timeouts, [9.0])

    def test_stream_text_uses_final_provider_usage_for_cache_observation(self) -> None:
        records: list[tuple[str, object]] = []
        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: iter([
                {"choices": [{"delta": {"content": "ok"}}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                        "prompt_tokens_details": {"cached_tokens": 8},
                    },
                },
            ]),
            acompletion_fn=unused_async_completion, egress_guard=allow_egress,
        )

        self.assertEqual(
            list(gateway.stream_text(
                [{"role": "user", "content": "stream"}],
                wire_attempt_sink=_WireAttemptSink(records),
            )),
            ["ok"],
        )

        self.assertEqual(records[-1], (
            "succeeded",
            (
                {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
                {"cache_read_input_tokens": 8},
            ),
        ))

    def test_metadata_stream_uses_final_provider_usage_for_cache_observation(self) -> None:
        records: list[tuple[str, object]] = []
        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: iter([
                {"choices": [{"delta": {"content": "ok"}}]},
                {
                    "choices": [],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "input_tokens_details": {
                            "cached_tokens": 6,
                            "cache_write_tokens": 1,
                        },
                    },
                },
            ]),
            acompletion_fn=unused_async_completion, egress_guard=allow_egress,
        )

        chunks = list(gateway.stream_text_with_metadata(
            [{"role": "user", "content": "stream"}],
            wire_attempt_sink=_WireAttemptSink(records),
        ))

        self.assertEqual([chunk.delta for chunk in chunks], ["ok", ""])
        self.assertEqual(records[-1], (
            "succeeded",
            (
                {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                {
                    "cache_read_input_tokens": 6,
                    "cache_creation_input_tokens": 1,
                },
            ),
        ))

    def test_stream_without_provider_usage_keeps_cache_observation_unavailable(self) -> None:
        records: list[tuple[str, object]] = []
        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: iter([
                {"choices": [{"delta": {"content": "ok"}}]},
            ]),
            acompletion_fn=unused_async_completion, egress_guard=allow_egress,
        )

        list(gateway.stream_text(
            [{"role": "user", "content": "stream"}],
            wire_attempt_sink=_WireAttemptSink(records),
        ))

        self.assertEqual(records[-1], ("succeeded", ({}, None)))

    def test_stream_close_records_one_cancelled_terminal_and_closes_provider_stream(self) -> None:
        provider_stream = ClosableStream()
        egress = _RecordingEgress()
        observations: list[tuple[str, BaseException | None]] = []
        wire_records: list[tuple[str, object]] = []
        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: provider_stream,
            acompletion_fn=unused_async_completion, egress_guard=egress,
            provider_attempt_observer=lambda status, error: observations.append((status, error)),
        )

        stream = gateway.stream_text(
            [{"role": "user", "content": "stream"}],
            wire_attempt_sink=_WireAttemptSink(wire_records),
        )
        self.assertEqual(next(stream), "first")
        stream.close()

        self.assertEqual(egress.leases[0].outcomes, [("cancelled", "consumer_cancelled")])
        self.assertEqual([item[0] for item in observations], ["cancelled"])
        self.assertEqual(provider_stream.close_calls, 1)
        self.assertEqual([item[0] for item in wire_records], ["dispatched", "consumer_cancelled"])

    def test_metadata_stream_close_records_one_cancelled_terminal(self) -> None:
        provider_stream = ClosableStream()
        egress = _RecordingEgress()
        observations: list[tuple[str, BaseException | None]] = []
        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: provider_stream,
            acompletion_fn=unused_async_completion, egress_guard=egress,
            provider_attempt_observer=lambda status, error: observations.append((status, error)),
        )

        stream = gateway.stream_text_with_metadata(
            [{"role": "user", "content": "stream"}],
        )
        self.assertEqual(next(stream).delta, "first")
        stream.close()

        self.assertEqual(egress.leases[0].outcomes, [("cancelled", "consumer_cancelled")])
        self.assertEqual([item[0] for item in observations], ["cancelled"])
        self.assertEqual(provider_stream.close_calls, 1)

    def test_stream_transport_failure_records_one_failed_terminal(self) -> None:
        egress = _RecordingEgress()
        observations: list[tuple[str, BaseException | None]] = []
        wire_records: list[tuple[str, object]] = []
        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: FailingStream(),
            acompletion_fn=unused_async_completion, egress_guard=egress,
            provider_attempt_observer=lambda status, error: observations.append((status, error)),
        )

        with self.assertRaisesRegex(TimeoutError, "stream timeout"):
            list(gateway.stream_text(
                [{"role": "user", "content": "stream"}],
                wire_attempt_sink=_WireAttemptSink(wire_records),
            ))

        self.assertEqual(egress.leases[0].outcomes, [("failed", "provider_stream_failed")])
        self.assertEqual([item[0] for item in observations], ["failed"])
        self.assertEqual(wire_records, [
            ("dispatched", None),
            ("failed_transport", "ai.provider_stream_failed"),
        ])

    def test_async_empty_response_does_not_issue_hidden_stream_retry(self) -> None:
        calls = 0
        egress = _RecordingEgress()

        async def empty_completion(**_kwargs):
            nonlocal calls
            calls += 1
            return {"choices": [{"message": {"content": ""}}]}

        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: None,
            acompletion_fn=empty_completion, egress_guard=egress,
        )

        with self.assertRaisesRegex(RuntimeError, "message.content"):
            asyncio.run(gateway.acomplete_text([{"role": "user", "content": "ping"}]))

        self.assertEqual(calls, 1)
        self.assertEqual(egress.leases[0].outcomes, [("succeeded", None)])

    def test_async_stream_close_records_one_cancelled_terminal(self) -> None:
        provider_stream = AsyncClosableStream()
        egress = _RecordingEgress()
        observations: list[tuple[str, BaseException | None]] = []

        async def stream_completion(**_kwargs):
            return provider_stream

        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: None,
            acompletion_fn=stream_completion, egress_guard=egress,
            provider_attempt_observer=lambda status, error: observations.append((status, error)),
        )

        async def consume_then_close() -> None:
            stream = gateway.astream_text([{"role": "user", "content": "stream"}])
            self.assertEqual(await anext(stream), "first")
            await stream.aclose()

        asyncio.run(consume_then_close())

        self.assertEqual(egress.leases[0].outcomes, [("cancelled", "consumer_cancelled")])
        self.assertEqual([item[0] for item in observations], ["cancelled"])
        self.assertEqual(provider_stream.close_calls, 1)

    def test_async_stream_uses_final_provider_usage_for_cache_observation(self) -> None:
        records: list[tuple[str, object]] = []

        async def stream_completion(**_kwargs):
            async def provider_stream():
                yield {"choices": [{"delta": {"content": "ok"}}]}
                yield {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 9,
                        "completion_tokens": 1,
                        "prompt_cache_hit_tokens": 4,
                        "prompt_cache_miss_tokens": 5,
                    },
                }
            return provider_stream()

        gateway = LiteLLMCompletionGateway(
            provider="deepseek", model="deepseek-chat",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: None,
            acompletion_fn=stream_completion, egress_guard=allow_egress,
        )

        async def consume() -> list[str]:
            return [
                chunk async for chunk in gateway.astream_text(
                    [{"role": "user", "content": "stream"}],
                    wire_attempt_sink=_WireAttemptSink(records),
                )
            ]

        self.assertEqual(asyncio.run(consume()), ["ok"])
        self.assertEqual(records[-1], (
            "succeeded",
            (
                {"input_tokens": 9, "output_tokens": 1, "total_tokens": 10},
                {"cache_read_input_tokens": 4, "cache_miss_input_tokens": 5},
            ),
        ))

    def test_async_stream_task_cancel_records_one_cancelled_terminal(self) -> None:
        egress = _RecordingEgress()
        observations: list[tuple[str, BaseException | None]] = []
        entered = asyncio.Event()
        provider_stream = BlockingAsyncStream(entered)

        async def stream_completion(**_kwargs):
            return provider_stream

        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: None,
            acompletion_fn=stream_completion, egress_guard=egress,
            provider_attempt_observer=lambda status, error: observations.append((status, error)),
        )

        async def cancel_consumer() -> None:
            async def consume() -> None:
                async for _chunk in gateway.astream_text(
                    [{"role": "user", "content": "stream"}],
                ):
                    pass

            task = asyncio.create_task(consume())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(cancel_consumer())

        self.assertEqual(egress.leases[0].outcomes, [("cancelled", "consumer_cancelled")])
        self.assertEqual([item[0] for item in observations], ["cancelled"])
        self.assertEqual(provider_stream.close_calls, 1)

    def test_structured_format_fallback_finishes_each_wire_attempt_once(self) -> None:
        completion = RejectingFirstResponseFormatCompletion(
            SeriesAnswerPayload,
            '{"answer":"ok","citations":[],"used_source_types":[]}',
        )
        egress = _RecordingEgress()
        observations: list[tuple[str, BaseException | None]] = []
        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=completion, acompletion_fn=unused_async_completion,
            egress_guard=egress,
            provider_attempt_observer=lambda status, error: observations.append((status, error)),
        )

        result = gateway.complete_structured(
            [{"role": "user", "content": "structured"}],
            response_model=SeriesAnswerPayload,
        )

        self.assertEqual(result.answer, "ok")
        self.assertEqual(
            [lease.outcomes for lease in egress.leases],
            [[("failed", "provider_request_failed")], [("succeeded", None)]],
        )
        self.assertEqual([item[0] for item in observations], ["failed", "succeeded"])

    def test_structured_format_fallback_propagates_wire_attempt_sink_per_real_wire(self) -> None:
        completion = RejectingFirstResponseFormatCompletion(
            SeriesAnswerPayload,
            '{"answer":"ok","citations":[],"used_source_types":[]}',
        )
        records: list[tuple[str, object]] = []
        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=completion, acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        result = gateway.complete_structured(
            [{"role": "user", "content": "structured"}],
            response_model=SeriesAnswerPayload,
            wire_attempt_sink=_WireAttemptSink(records),
        )

        self.assertEqual(result.answer, "ok")
        self.assertEqual(
            [record[0] for record in records],
            [
                "dispatched", "handler", "failed_transport",
                "dispatched", "handler", "succeeded",
            ],
        )

    def test_async_structured_propagates_wire_attempt_sink(self) -> None:
        records: list[tuple[str, object]] = []

        async def completion(**_kwargs):
            return {
                "choices": [{"message": {"content": '{"answer":"ok","citations":[],"used_source_types":[]}'}}],
            }

        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: None, acompletion_fn=completion,
            egress_guard=allow_egress,
        )

        result = asyncio.run(gateway.acomplete_structured(
            [{"role": "user", "content": "structured"}],
            response_model=SeriesAnswerPayload,
            wire_attempt_sink=_WireAttemptSink(records),
        ))

        self.assertEqual(result.answer, "ok")
        self.assertEqual([record[0] for record in records], ["dispatched", "succeeded"])

    def test_async_structured_validation_retry_records_each_real_wire(self) -> None:
        records: list[tuple[str, object]] = []
        calls = 0

        async def completion(**_kwargs):
            nonlocal calls
            calls += 1
            content = (
                "not-json"
                if calls == 1
                else '{"answer":"ok","citations":[],"used_source_types":[]}'
            )
            return {"choices": [{"message": {"content": content}}]}

        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: None, acompletion_fn=completion,
            egress_guard=allow_egress,
        )

        result = asyncio.run(gateway.acomplete_structured(
            [{"role": "user", "content": "structured"}],
            response_model=SeriesAnswerPayload,
            wire_attempt_sink=_WireAttemptSink(records),
        ))

        self.assertEqual(result.answer, "ok")
        self.assertEqual(calls, 2)
        self.assertEqual(
            [record[0] for record in records],
            ["dispatched", "succeeded", "dispatched", "succeeded"],
        )

    def test_structured_wire_attempt_dispatch_failure_prevents_provider_call(self) -> None:
        calls = 0
        egress = _RecordingEgress()
        records: list[tuple[str, object]] = []

        def completion(**_kwargs):
            nonlocal calls
            calls += 1
            return {"choices": [{"message": {"content": "must-not-run"}}]}

        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=completion, acompletion_fn=unused_async_completion,
            egress_guard=egress,
        )

        with self.assertRaisesRegex(RuntimeError, "dispatch persistence"):
            gateway.complete_structured(
                [{"role": "user", "content": "structured"}],
                response_model=SeriesAnswerPayload,
                wire_attempt_sink=_WireAttemptSink(records, fail_begin=True),
            )

        self.assertEqual(calls, 0)
        self.assertEqual(records, [("dispatched", None)])
        self.assertEqual(egress.leases[0].outcomes, [("not_sent", "model_attempt_dispatch_failed")])

    def test_async_structured_wire_attempt_dispatch_failure_prevents_provider_call(self) -> None:
        calls = 0
        egress = _RecordingEgress()
        records: list[tuple[str, object]] = []

        async def completion(**_kwargs):
            nonlocal calls
            calls += 1
            return {"choices": [{"message": {"content": "must-not-run"}}]}

        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: None, acompletion_fn=completion,
            egress_guard=egress,
        )

        async def invoke() -> None:
            with self.assertRaisesRegex(RuntimeError, "dispatch persistence"):
                await gateway.acomplete_structured(
                    [{"role": "user", "content": "structured"}],
                    response_model=SeriesAnswerPayload,
                    wire_attempt_sink=_WireAttemptSink(records, fail_begin=True),
                )

        asyncio.run(invoke())
        self.assertEqual(calls, 0)
        self.assertEqual(records, [("dispatched", None)])
        self.assertEqual(egress.leases[0].outcomes, [("not_sent", "model_attempt_dispatch_failed")])

    def test_structured_retry_respects_explicit_wire_attempt_budget(self) -> None:
        calls = 0

        def invalid_completion(**_kwargs):
            nonlocal calls
            calls += 1
            return {"choices": [{"message": {"content": "not-json"}}]}

        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=invalid_completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        with self.assertRaisesRegex(RuntimeError, "wire attempt"):
            gateway.complete_structured(
                [{"role": "user", "content": "structured"}],
                response_model=SeriesAnswerPayload,
                retries=5,
                max_wire_attempts=2,
            )

        self.assertEqual(calls, 2)

    def test_async_structured_retry_respects_explicit_wire_attempt_budget(self) -> None:
        calls = 0

        async def invalid_completion(**_kwargs):
            nonlocal calls
            calls += 1
            return {"choices": [{"message": {"content": "not-json"}}]}

        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=lambda **_kwargs: None,
            acompletion_fn=invalid_completion,
            egress_guard=allow_egress,
        )

        async def invoke() -> None:
            with self.assertRaisesRegex(RuntimeError, "wire attempt"):
                await gateway.acomplete_structured(
                    [{"role": "user", "content": "structured"}],
                    response_model=SeriesAnswerPayload,
                    retries=5,
                    max_wire_attempts=2,
                )

        asyncio.run(invoke())
        self.assertEqual(calls, 2)

    def test_completion_usage_observes_openai_chat_cached_tokens_without_response_body(self) -> None:
        gateway = self._usage_gateway({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 8, "ignored": "secret"},
            },
            "secret": "must-not-leak",
        })

        text, usage, cache = gateway.complete_text_with_usage([{"role": "user", "content": "ping"}])

        self.assertEqual(text, "ok")
        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
        self.assertEqual(cache, {"cache_read_input_tokens": 8})

    def test_wire_attempt_is_dispatched_after_egress_and_receives_sanitized_terminal_metadata(self) -> None:
        order: list[tuple[str, object]] = []
        lease = _RecordingLease()

        def egress(_purpose, _categories, _payload_bytes):
            order.append(("authorized", None))
            return lease

        def completion(**_kwargs):
            order.append(("wire", None))
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                    "prompt_cache_hit_tokens": 3,
                    "prompt_cache_miss_tokens": 2,
                },
            }

        gateway = LiteLLMCompletionGateway(
            provider="deepseek", model="deepseek-chat",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=completion, acompletion_fn=unused_async_completion,
            egress_guard=egress,
        )

        gateway.complete_text_with_usage(
            [{"role": "user", "content": "private"}],
            wire_attempt_sink=_WireAttemptSink(order),
        )

        self.assertEqual(
            [item[0] for item in order],
            ["authorized", "dispatched", "handler", "wire", "succeeded"],
        )
        self.assertEqual(order[-1][1], (
            {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            {"cache_read_input_tokens": 3, "cache_miss_input_tokens": 2},
        ))
        self.assertEqual(lease.outcomes, [("succeeded", None)])

    def test_wire_attempt_dispatch_failure_prevents_provider_call(self) -> None:
        calls = 0
        egress = _RecordingEgress()
        records: list[tuple[str, object]] = []

        def completion(**_kwargs):
            nonlocal calls
            calls += 1
            return {"choices": [{"message": {"content": "must-not-run"}}]}

        gateway = LiteLLMCompletionGateway(
            provider="openai", model="test-model",
            base_url="https://example.invalid/v1", api_key="test-key",
            completion_fn=completion, acompletion_fn=unused_async_completion,
            egress_guard=egress,
        )

        with self.assertRaisesRegex(RuntimeError, "dispatch persistence"):
            gateway.complete_text(
                [{"role": "user", "content": "private"}],
                wire_attempt_sink=_WireAttemptSink(records, fail_begin=True),
            )

        self.assertEqual(calls, 0)
        self.assertEqual(egress.leases[0].outcomes, [("not_sent", "model_attempt_dispatch_failed")])

    def test_completion_usage_observes_openai_responses_cached_tokens(self) -> None:
        gateway = self._usage_gateway({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "input_tokens": 12,
                "output_tokens": 3,
                "input_tokens_details": {"cached_tokens": 6, "cache_write_tokens": 2},
            },
        })

        _text, usage, cache = gateway.complete_text_with_usage([{"role": "user", "content": "ping"}])

        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
        self.assertEqual(cache, {
            "cache_read_input_tokens": 6,
            "cache_creation_input_tokens": 2,
        })

    def test_completion_usage_observes_anthropic_and_litellm_root_cache_counters(self) -> None:
        gateway = self._usage_gateway({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "input_tokens": 12,
                "output_tokens": 3,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 4,
            },
        })

        _text, usage, cache = gateway.complete_text_with_usage([{"role": "user", "content": "ping"}])

        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
        self.assertEqual(cache, {"cache_read_input_tokens": 7, "cache_creation_input_tokens": 4})

    def test_completion_usage_observes_deepseek_hit_miss_only_when_exactly_consistent(self) -> None:
        gateway = self._usage_gateway({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "prompt_cache_hit_tokens": 9,
                "prompt_cache_miss_tokens": 3,
            },
        })

        _text, _usage, cache = gateway.complete_text_with_usage([{"role": "user", "content": "ping"}])

        self.assertEqual(cache, {"cache_read_input_tokens": 9, "cache_miss_input_tokens": 3})

    def test_completion_usage_marks_inconsistent_deepseek_cache_counters_unavailable(self) -> None:
        gateway = self._usage_gateway({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 12,
                "prompt_cache_hit_tokens": 9,
                "prompt_cache_miss_tokens": 2,
            },
        })

        text, usage, cache = gateway.complete_text_with_usage(
            [{"role": "user", "content": "ping"}]
        )

        self.assertEqual(text, "ok")
        self.assertEqual(usage["input_tokens"], 12)
        self.assertIsNone(cache)

    def test_completion_usage_marks_conflicting_normalized_cache_counters_unavailable(self) -> None:
        gateway = self._usage_gateway({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "input_tokens": 12,
                "output_tokens": 3,
                "cache_read_input_tokens": 7,
                "input_tokens_details": {"cached_tokens": 6},
            },
        })

        _text, _usage, cache = gateway.complete_text_with_usage(
            [{"role": "user", "content": "ping"}]
        )

        self.assertIsNone(cache)

    def test_completion_usage_does_not_infer_cache_without_explicit_provider_fields(self) -> None:
        gateway = self._usage_gateway({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        })

        _text, usage, cache = gateway.complete_text_with_usage([{"role": "user", "content": "ping"}])

        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
        self.assertIsNone(cache)

    def test_empty_nonstream_response_does_not_issue_a_second_provider_request(self) -> None:
        calls = 0

        def completion(**_kwargs):
            nonlocal calls
            calls += 1
            return {"choices": [{"message": {"content": ""}}], "usage": {}}

        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        with self.assertRaisesRegex(RuntimeError, "message.content"):
            gateway.complete_text_with_usage([{"role": "user", "content": "ping"}])

        self.assertEqual(calls, 1)

    def _usage_gateway(self, response: dict[str, object]) -> LiteLLMCompletionGateway:
        return LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=lambda **_kwargs: response,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

    def test_delayed_provider_honors_propagated_deadline_without_retry(self) -> None:
        completion = DeadlineRespectingCompletion(delay_seconds=0.03)
        gateway = LiteLLMCompletionGateway(
            provider="openai",
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            completion_fn=completion,
            acompletion_fn=unused_async_completion,
            egress_guard=allow_egress,
        )

        started_at = monotonic()
        with self.assertRaisesRegex(TimeoutError, "controlled provider timeout"):
            gateway.complete_structured(
                [{"role": "user", "content": "slow"}],
                response_model=SeriesAnswerPayload,
                retries=2,
                timeout=0.01,
            )
        elapsed = monotonic() - started_at

        self.assertEqual(completion.calls, 1)
        self.assertLess(elapsed, 0.1)


class CapturingCompletion:
    def __init__(self, content: str) -> None:
        self._content = content
        self.messages: list[list[dict[str, object]]] = []
        self.response_formats: list[object] = []
        self.api_bases: list[str] = []
        self.reasoning_efforts: list[object] = []
        self.allowed_openai_params: list[object] = []
        self.models: list[str] = []
        self.timeouts: list[object] = []

    def __call__(self, **kwargs):
        self.messages.append(list(kwargs["messages"]))
        self.response_formats.append(kwargs.get("response_format"))
        self.api_bases.append(kwargs["api_base"])
        self.reasoning_efforts.append(kwargs.get("reasoning_effort"))
        self.allowed_openai_params.append(kwargs.get("allowed_openai_params"))
        self.models.append(kwargs["model"])
        self.timeouts.append(kwargs.get("timeout"))
        return {"choices": [{"message": {"content": self._content}}]}


class TimingOutCompletion:
    def __init__(self) -> None:
        self.calls = 0
        self.timeouts: list[object] = []

    def __call__(self, **kwargs):
        self.calls += 1
        self.timeouts.append(kwargs.get("timeout"))
        raise TimeoutError("controlled deadline")


class StreamingCompletion:
    def __init__(self) -> None:
        self.timeouts: list[object] = []

    def __call__(self, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return iter([{"choices": [{"delta": {"content": "ok"}}]}])


class ClosableStream:
    def __init__(self) -> None:
        self._sent = False
        self.close_calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._sent:
            raise StopIteration
        self._sent = True
        return {"choices": [{"delta": {"content": "first"}}]}

    def close(self) -> None:
        self.close_calls += 1


class FailingStream:
    def __init__(self) -> None:
        self._sent = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._sent:
            self._sent = True
            return {"choices": [{"delta": {"content": "first"}}]}
        raise TimeoutError("stream timeout")


class AsyncClosableStream:
    def __init__(self) -> None:
        self._sent = False
        self.close_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return {"choices": [{"delta": {"content": "first"}}]}

    async def aclose(self) -> None:
        self.close_calls += 1


class BlockingAsyncStream:
    def __init__(self, entered: asyncio.Event) -> None:
        self._entered = entered
        self._sent = False
        self.close_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._sent:
            self._sent = True
            return {"choices": [{"delta": {"content": "first"}}]}
        self._entered.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1


class DeadlineRespectingCompletion:
    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        timeout = float(kwargs["timeout"])
        sleep(min(self.delay_seconds, timeout))
        if self.delay_seconds > timeout:
            raise TimeoutError("controlled provider timeout")
        return {"choices": [{"message": {"content": "{}"}}]}


class RejectingFirstResponseFormatCompletion(CapturingCompletion):
    def __init__(self, rejected_format: object, content: str) -> None:
        super().__init__(content)
        self._rejected_format = rejected_format

    def __call__(self, **kwargs):
        self.messages.append(list(kwargs["messages"]))
        response_format = kwargs.get("response_format")
        self.response_formats.append(response_format)
        if response_format is self._rejected_format:
            raise RuntimeError("response_format json_schema is not supported")
        return {"choices": [{"message": {"content": self._content}}]}


class RejectingResponseFormatsCompletion(CapturingCompletion):
    def __call__(self, **kwargs):
        self.messages.append(list(kwargs["messages"]))
        response_format = kwargs.get("response_format")
        self.response_formats.append(response_format)
        if response_format is not None:
            raise RuntimeError("response_format is not supported")
        return {"choices": [{"message": {"content": self._content}}]}


class RejectingBaseModelResponseFormatsCompletion(CapturingCompletion):
    def __init__(self, content: str) -> None:
        super().__init__(content)
        self._has_seen_schema_success = False

    def __call__(self, **kwargs):
        self.messages.append(list(kwargs["messages"]))
        response_format = kwargs.get("response_format")
        self.response_formats.append(response_format)
        if isinstance(response_format, type) and issubclass(response_format, BaseModel):
            if self._has_seen_schema_success:
                raise RuntimeError("response_format json_schema is not supported")
            self._has_seen_schema_success = True
        return {"choices": [{"message": {"content": self._content}}]}


class RejectingReasoningEffortCompletion:
    def __call__(self, **kwargs):
        del kwargs
        raise RuntimeError("openai does not support parameters: ['reasoning_effort']")


async def unused_async_completion(**kwargs):
    del kwargs
    raise AssertionError("async completion should not be called")


if __name__ == "__main__":
    unittest.main()
