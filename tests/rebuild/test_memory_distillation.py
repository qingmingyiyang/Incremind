from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from core.companion_core.models import CompanionMessage, ConversationEpisodeProjection
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core.memory_distillation import (
    DistillConversationEpisodeToMemoryProposal,
    ObjectStoreMemoryDistillationDiaryRepository,
    serialize_memory_distillation_result,
)
from core.storage_provider import JsonObjectStore


@dataclass
class _Messages:
    values: dict[str, CompanionMessage]

    def get_message(self, message_id: str) -> CompanionMessage | None:
        return self.values.get(message_id)


@dataclass
class _Candidates:
    values: dict[str, dict[str, object]]

    def save(self, candidate: dict[str, object]) -> dict[str, object]:
        self.values[str(candidate["id"])] = dict(candidate)
        return dict(candidate)

    def get(self, candidate_id: str) -> dict[str, object] | None:
        candidate = self.values.get(candidate_id)
        return dict(candidate) if candidate else None

    def list_by_project(self, project_id: str) -> tuple[dict[str, object], ...]:
        return tuple(item for item in self.values.values() if item["project_id"] == project_id)


def _message(message_id: str, role: str, *, revision: int = 1, content: str | None = None) -> CompanionMessage:
    return CompanionMessage(
        message_id=message_id, request_id="request-1", session_id="session-old", context_epoch=1,
        project_id="project-a", role=role, status="completed",
        content=content or ("用户问了长期的项目决定" if role == "user" else "项目将使用本地 SQLite 作为唯一写入权威，并保留审核流程。"),
        created_at="2026-08-01T10:00:00+00:00", provider_mode="local", revision=revision, memory_review={},
    )


def _episode() -> ConversationEpisodeProjection:
    return ConversationEpisodeProjection(
        episode_id="episode-1", agent_id="companion.chat", project_id="project-a", session_id="session-old",
        context_epoch=1, request_id="request-1", user_message_id="message-user", user_message_revision=1,
        assistant_message_id="message-assistant", assistant_message_revision=1,
        summary="用户：用户问了长期的项目决定\n助手：项目将使用本地 SQLite 作为唯一写入权威，并保留审核流程。",
        occurred_at="2026-08-01T10:00:00+00:00",
    )


def _service(messages: _Messages, candidates: _Candidates) -> DistillConversationEpisodeToMemoryProposal:
    return DistillConversationEpisodeToMemoryProposal(
        messages=messages, candidates=candidates, now="2026-08-31T10:00:00+00:00",
    )


def test_distillation_creates_only_reviewable_grounded_candidate() -> None:
    messages = _Messages({"message-user": _message("message-user", "user"), "message-assistant": _message("message-assistant", "assistant")})
    candidates = _Candidates({})

    result = _service(messages, candidates).execute(_episode())

    assert result.status == "pending_review"
    assert result.candidate_id
    saved = candidates.values[result.candidate_id]
    assert saved["status"] == "pending_review"
    assert saved["target_layer"] == "atom"
    assert saved["review"] == {
        "requires_user_confirmation": True, "auto_promote_allowed": False,
        "reason": "基于已验证会话事实生成的候选记忆，需用户审核后才能进入长期记忆。",
        "reviewed_by": None, "reviewed_at": None,
    }
    assert saved["provenance"]["conversation_episode_id"] == "episode-1"
    assert result.diary.reason_codes == ("eligible_grounded_episode", "requires_user_review")
    serialized = serialize_memory_distillation_result(result)
    assert "proposed_content" not in str(serialized)
    assert serialized["memory_publication_state"] == "candidate_created_not_published"


def test_distillation_skips_deleted_or_revision_drifted_source() -> None:
    candidates = _Candidates({})
    messages = _Messages({"message-user": _message("message-user", "user"), "message-assistant": _message("message-assistant", "assistant", revision=2)})

    result = _service(messages, candidates).execute(_episode())

    assert result.status == "skipped"
    assert result.candidate_id is None
    assert result.diary.reason_codes == ("stale_source",)
    assert candidates.values == {}


def test_distillation_blocks_sensitive_source_before_candidate_persistence() -> None:
    episode = replace(_episode(), summary="用户：请记录\n助手：api_key=super-secret-value 应该保存")
    messages = _Messages({
        "message-user": _message("message-user", "user", content="请记录"),
        "message-assistant": _message("message-assistant", "assistant", content="api_key=super-secret-value 应该保存"),
    })
    candidates = _Candidates({})

    result = _service(messages, candidates).execute(episode)

    assert result.status == "skipped"
    assert result.diary.reason_codes == ("redaction_required",)
    assert candidates.values == {}


def test_distillation_detects_conflicting_current_memory_without_overwriting() -> None:
    messages = _Messages({"message-user": _message("message-user", "user"), "message-assistant": _message("message-assistant", "assistant")})
    candidates = _Candidates({})
    episode = replace(_episode(), summary="用户：persistent project decision\n助手：Keep the local SQLite authority and retain a human review workflow.")
    messages = _Messages({
        "message-user": _message("message-user", "user", content="persistent project decision"),
        "message-assistant": _message("message-assistant", "assistant", content="Keep the local SQLite authority and retain a human review workflow."),
    })
    current = {"id": "published-1", "content": "Keep the local SQLite authority but remove the human review workflow."}

    result = _service(messages, candidates).execute(episode, published_memory=(current,))

    assert result.status == "skipped"
    assert result.diary.reason_codes == ("conflict_current",)
    assert result.diary.conflicts == ("published-1",)
    assert candidates.values == {}


def test_distillation_replays_existing_candidate_without_new_write() -> None:
    messages = _Messages({"message-user": _message("message-user", "user"), "message-assistant": _message("message-assistant", "assistant")})
    candidates = _Candidates({})
    first = _service(messages, candidates).execute(_episode())

    replayed = _service(messages, candidates).execute(_episode())

    assert replayed.candidate_id == first.candidate_id
    assert replayed.status == "pending_review"
    assert replayed.diary.reason_codes == ("duplicate_candidate",)
    assert len(candidates.values) == 1


def test_distillation_candidate_satisfies_existing_repository_review_contract(tmp_path: Path) -> None:
    messages = _Messages({"message-user": _message("message-user", "user"), "message-assistant": _message("message-assistant", "assistant")})
    repository = ObjectStoreMemoryCandidateRepository(JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library"))

    result = DistillConversationEpisodeToMemoryProposal(
        messages=messages, candidates=repository, now="2026-08-31T10:00:00+00:00",
    ).execute(_episode())

    persisted = repository.get(str(result.candidate_id))
    assert persisted is not None
    assert persisted["status"] == "pending_review"
    assert persisted["review"]["auto_promote_allowed"] is False


def test_dream_diary_keeps_auditable_metadata_without_proposal_body(tmp_path: Path) -> None:
    messages = _Messages({"message-user": _message("message-user", "user"), "message-assistant": _message("message-assistant", "assistant")})
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    result = DistillConversationEpisodeToMemoryProposal(
        messages=messages,
        candidates=ObjectStoreMemoryCandidateRepository(store),
        now="2026-08-31T10:00:00+00:00",
    ).execute(_episode())

    diary = ObjectStoreMemoryDistillationDiaryRepository(store).record(
        command_id="distill-episode-1",
        current_session_id="session-current",
        episode=_episode(),
        result=result,
        created_at="2026-08-31T10:00:00+00:00",
    )

    assert diary["disposition"] == "pending_review"
    assert diary["candidate_id"] == result.candidate_id
    assert diary["memory_publication_state"] == "not_published"
    assert diary["auto_publish_allowed"] is False
    assert diary["content_included"] is False
    assert "proposed_content" not in str(diary)
