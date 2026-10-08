"""真实 Gateway 合同；仅隔离外部 completion、外发端口与 wire sink。"""
import pytest

from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway, clear_structured_mode_cache
from tests.backend.unit.llm.test_litellm_gateway import (
    CapturingCompletion,
    SeriesAnswerPayload,
    _RecordingEgress,
    _WireAttemptSink,
    unused_async_completion,
)


def test_structured_stream_token_budget_rejects_before_any_wire_boundary():
    clear_structured_mode_cache()
    completion = CapturingCompletion('unused synthetic output')
    egress = _RecordingEgress()
    wire_records = []
    deltas = []
    gateway = LiteLLMCompletionGateway(
        provider='openai', model='test-model', base_url='https://example.invalid/v1',
        api_key='synthetic-test-key', completion_fn=completion,
        acompletion_fn=unused_async_completion, egress_guard=egress,
        context_window_tokens=64, reserved_output_tokens=16,
    )

    with pytest.raises(RuntimeError, match='Token 硬预算'):
        gateway.stream_structured_with_usage(
            [{'role': 'user', 'content': 'x' * 500}], response_model=SeriesAnswerPayload,
            on_delta=deltas.append, validate_current=lambda: None, max_tokens=16,
            wire_attempt_sink=_WireAttemptSink(wire_records),
        )

    # 只比较计数，RED 输出不会包含请求内容或测试密钥。
    observed = {
        'begin': sum(kind == 'dispatched' for kind, _ in wire_records),
        'invoke': sum(kind == 'handler' for kind, _ in wire_records),
        'completion': len(completion.messages),
        'delta': len(deltas),
        'failed_terminal': sum(kind == 'failed_transport' for kind, _ in wire_records),
    }
    assert observed == {'begin': 0, 'invoke': 0, 'completion': 0, 'delta': 0, 'failed_terminal': 0}
    assert egress.leases == []
    assert wire_records == []
