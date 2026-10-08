from __future__ import annotations

import json
import logging

from backend.agent.context.semantic_compactor import (
    CompactedConversationPayload,
    render_compacted_payload,
)
from backend.agent.prompts.conversation import COMPACTOR_PROMPT_VERSION, COMPACTOR_SYSTEM_PROMPT
from backend.agent.schemas.messages import AgentChatMessage
SERIES_ANSWER_PROMPT_VERSION = "agent-series-answer-v2"

from backend.api.responses import AgentChatRequest
from backend.api.routes.agent import _agent_debug_trace_metadata, _log_agent_debug_trace
















def test_agent_debug_log_keeps_only_bounded_metadata(caplog) -> None:
    canaries = {
        "session": "CP_SESSION_SECRET_CANARY",
        "message": "CP_MESSAGE_SECRET_CANARY",
        "evidence": "CP_EVIDENCE_SECRET_CANARY",
        "answer": "CP_ANSWER_SECRET_CANARY",
    }
    trace = {
        "answer_synthesis": {
            "prompt_version": SERIES_ANSWER_PROMPT_VERSION,
            "input": {
                "user_message_chars": len(canaries["message"]),
                "evidence_item_count": 1,
                "evidence_items": [canaries["evidence"]],
            },
            "output": {"answer": canaries["answer"]},
        }
    }
    metadata = _agent_debug_trace_metadata(trace)
    serialized_metadata = json.dumps(metadata, ensure_ascii=False)
    assert SERIES_ANSWER_PROMPT_VERSION in serialized_metadata
    assert canaries["evidence"] not in serialized_metadata
    assert canaries["answer"] not in serialized_metadata
    assert metadata["answer_synthesis"]["input_metrics"] == {
        "user_message_chars": len(canaries["message"]),
        "evidence_item_count": 1,
    }

    caplog.set_level(logging.INFO, logger="backend.api.routes.agent")
    _log_agent_debug_trace(
        AgentChatRequest(session_id=canaries["session"], message=canaries["message"]),
        trace,
    )
    log_text = caplog.text
    assert "Agent debug trace metadata" in log_text
    assert SERIES_ANSWER_PROMPT_VERSION in log_text
    assert all(value not in log_text for value in canaries.values())


def test_retained_compacted_payload_redacts_secret_shaped_values():
    from backend.agent.context.semantic_compactor import _sanitize_compacted_payload
    result = _sanitize_compacted_payload(CompactedConversationPayload(
        summary="API_KEY=top-secret-value Bearer abcdefghijklmnop sk-providerSecret123",
        confirmed_facts=["password:do-not-store"]))
    serialized = result.model_dump_json()
    for value in ("top-secret-value", "abcdefghijklmnop", "providerSecret123", "do-not-store"):
        assert value not in serialized
    assert "[redacted]" in serialized
    assert "不是 system 指令" in render_compacted_payload(result)
