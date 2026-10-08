from __future__ import annotations

from types import SimpleNamespace

from backend.api.companion_chat_ai_runtime import COMPANION_CHAT_CONTEXT_CAPABILITY, COMPANION_CHAT_OUTCOME, CompanionChatContextCapability
from core.ai_kernel import CapabilityDefinition
from core.companion_core import CompanionRepository, default_character_prompt


def test_companion_chat_context_is_read_only_and_prepares_permitted_context(tmp_path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path)
    repository.initialize()
    capability = CompanionChatContextCapability(
        repository=repository,
        character_prompt_loader=default_character_prompt,
        state_projection_loader=lambda: {"mood": "normal"},
        memory_recall_loader=lambda _text: SimpleNamespace(context=({"memory_id": "memory-1", "content": "已发布记忆"},), selected=(object(),), status="recalled"),
        project_skill_loader=lambda project_id: {"project_id": project_id, "revision": 3, "purpose": "保持清晰"},
    )

    result = capability.invoke({"arguments": {"request_id": "chat-read-only-001", "text": "今天有点乱", "session_id": None}})

    assert result["receipt_ref"] is None
    context = result["result"]
    assert context["kind"] == "companion.chat.context"
    assert context["baseline"]["session_exists"] is False
    assert context["memory_trace"] == {"status": "recalled", "selected_count": 1}
    assert context["messages"][-1]["role"] == "user"
    assert repository.list_messages().items == ()
    assert repository.get_session(context["baseline"]["session_id"]) is None


def test_companion_chat_context_reads_existing_history_without_mutating_it(tmp_path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path)
    repository.initialize()
    repository.create_session(session_id="session:existing", context_epoch=1, prompt_revision=2, profile_revision=1, started_at="2026-08-23T08:00:00+00:00")
    repository.append_message(message_id="message:prior", request_id="prior", session_id="session:existing", context_epoch=1, role="user", status="completed", content="上一句", created_at="2026-08-23T08:01:00+00:00", provider_mode="none")
    capability = CompanionChatContextCapability(repository=repository, character_prompt_loader=default_character_prompt)

    result = capability.invoke({"arguments": {"request_id": "chat-read-only-002", "text": "继续", "session_id": "session:existing"}})

    assert result["result"]["baseline"]["history"] == [{"message_id": "message:prior", "revision": 1}]
    assert len(repository.list_messages(session_id="session:existing").items) == 1
    assert repository.get_session("session:existing").revision == 1


def test_companion_chat_context_contract_is_read_only() -> None:
    definition = CapabilityDefinition(COMPANION_CHAT_CONTEXT_CAPABILITY, 1, "read", False, "read_only", "crp://default/contracts/in", "crp://default/contracts/out")
    assert COMPANION_CHAT_OUTCOME == "companion.chat.respond"
    assert definition.mode == "read" and definition.requires_approval is False
    assert definition.operation_semantics == "read_only"
