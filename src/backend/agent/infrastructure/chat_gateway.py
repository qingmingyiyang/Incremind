from __future__ import annotations

from collections.abc import Iterator

from backend.agent.ports import ChatGateway, StructuredResponseT
from backend.agent.schemas.chat_stream import ChatCompletionStreamChunk
from backend.agent.schemas.messages import AgentChatMessage


AGENT_MODEL_REQUEST_TIMEOUT_SECONDS = 45.0


class LiteLLMChatGateway(ChatGateway):
    """Legacy AgentGraph shape over an injected, already-governed gateway.

    This adapter owns no endpoint, credential or transport construction. The
    production Agent API is retired; tests and migration tools may inject a
    compatibility gateway explicitly while remaining callers move to AI Turn.
    """

    def __init__(self, gateway: object) -> None:
        self._gateway = gateway

    def create_text_completion(self, messages: list[AgentChatMessage]) -> str:
        return self._gateway.complete_text(
            _dump_messages(messages),
            timeout=AGENT_MODEL_REQUEST_TIMEOUT_SECONDS,
        )

    def create_text_completion_stream(self, messages: list[AgentChatMessage]) -> Iterator[str]:
        return self._gateway.stream_text(
            _dump_messages(messages),
            timeout=AGENT_MODEL_REQUEST_TIMEOUT_SECONDS,
        )

    def create_text_completion_stream_with_metadata(
        self,
        messages: list[AgentChatMessage],
    ) -> Iterator[ChatCompletionStreamChunk]:
        return self._gateway.stream_text_with_metadata(
            _dump_messages(messages),
            timeout=AGENT_MODEL_REQUEST_TIMEOUT_SECONDS,
        )

    def create_structured_completion(
        self,
        messages: list[AgentChatMessage],
        response_model: type[StructuredResponseT],
    ) -> StructuredResponseT:
        return self._gateway.complete_structured(
            _dump_messages(messages),
            response_model=response_model,
            timeout=AGENT_MODEL_REQUEST_TIMEOUT_SECONDS,
        )


def _dump_messages(messages: list[AgentChatMessage]) -> list[dict[str, object]]:
    return [message.model_dump() for message in messages]
