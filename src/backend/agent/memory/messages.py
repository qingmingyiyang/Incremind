from __future__ import annotations

from math import ceil

from backend.agent.schemas.messages import AgentChatMessage


def render_memory_messages(messages: list[AgentChatMessage]) -> str:
    lines = [
        f"{message.role}: {message.content.strip()}"
        for message in messages
        if isinstance(message.content, str) and message.content.strip()
    ]
    return "\n".join(lines).strip()


def estimate_memory_message_tokens(messages: list[AgentChatMessage]) -> int:
    text = render_memory_messages(messages)
    if not text:
        return 0
    return max(1, ceil(len(text.encode("utf-8")) / 3))
