from __future__ import annotations

from backend.agent.infrastructure.chat_gateway import (
    AGENT_MODEL_REQUEST_TIMEOUT_SECONDS,
    LiteLLMChatGateway,
)
from backend.agent.schemas.messages import AgentChatMessage
from pydantic import BaseModel


class VideoAnswerPayload(BaseModel):
    answer: str


class _CapturingInnerGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float | None]] = []

    def complete_text(self, messages, *, timeout=None):
        del messages
        self.calls.append(("text", timeout))
        return "ok"

    def stream_text(self, messages, *, timeout=None):
        del messages
        self.calls.append(("stream", timeout))
        return iter(["ok"])

    def stream_text_with_metadata(self, messages, *, timeout=None):
        del messages
        self.calls.append(("metadata_stream", timeout))
        return iter([])

    def complete_structured(self, messages, *, response_model, timeout=None):
        del messages
        self.calls.append(("structured", timeout))
        return response_model(answer="ok")


def test_agent_gateway_applies_one_deadline_to_every_model_call_shape() -> None:
    inner = _CapturingInnerGateway()
    gateway = LiteLLMChatGateway(inner)
    messages = [AgentChatMessage(role="user", content="hello")]

    assert gateway.create_text_completion(messages) == "ok"
    assert list(gateway.create_text_completion_stream(messages)) == ["ok"]
    assert list(gateway.create_text_completion_stream_with_metadata(messages)) == []
    assert gateway.create_structured_completion(messages, VideoAnswerPayload).answer == "ok"

    assert inner.calls == [
        ("text", AGENT_MODEL_REQUEST_TIMEOUT_SECONDS),
        ("stream", AGENT_MODEL_REQUEST_TIMEOUT_SECONDS),
        ("metadata_stream", AGENT_MODEL_REQUEST_TIMEOUT_SECONDS),
        ("structured", AGENT_MODEL_REQUEST_TIMEOUT_SECONDS),
    ]
    assert 0 < AGENT_MODEL_REQUEST_TIMEOUT_SECONDS <= 60
