from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.companion_core.conversation_recall import ConversationRecallService
from core.companion_core.history import CompanionHistoryService
from core.companion_core.repository import CompanionRepository


NOW = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)


def _repository(path: Path) -> CompanionRepository:
    return CompanionRepository(path, now=lambda: NOW)


def _session(repository: CompanionRepository, session_id: str, *, project_id: str = "default") -> None:
    repository.create_session(
        session_id=session_id, context_epoch=1, prompt_revision=1, profile_revision=1,
        project_id=project_id, started_at="2026-08-31T08:00:00+00:00",
    )


def _pair(
    repository: CompanionRepository,
    *,
    suffix: str,
    session_id: str,
    project_id: str = "default",
    user_status: str = "completed",
    assistant_status: str = "completed",
    user_content: str = "用户问题",
    assistant_content: str = "助手回答",
) -> tuple[str, str]:
    request_id = f"request:recall:{suffix}"
    user_id = f"message:user:{suffix}"
    assistant_id = f"message:assistant:{suffix}"
    repository.append_message(
        message_id=user_id, request_id=request_id, session_id=session_id, context_epoch=1,
        project_id=project_id, role="user", status=user_status,
        content=user_content if user_status == "completed" else "",
        provider_mode="none", created_at=f"2026-08-31T08:0{suffix[-1]}:00+00:00",
    )
    repository.append_message(
        message_id=assistant_id, request_id=request_id, session_id=session_id, context_epoch=1,
        project_id=project_id, role="assistant", status=assistant_status,
        content=assistant_content if assistant_status == "completed" else "",
        provider_mode="local", created_at=f"2026-08-31T08:1{suffix[-1]}:00+00:00",
    )
    return user_id, assistant_id


def test_rebuild_projects_only_completed_private_chat_pairs_and_recall_excludes_current_session(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")
    _session(repository, "session:past")
    _session(repository, "session:current")
    _session(repository, "session:other-project", project_id="project:other")
    _pair(repository, suffix="001", session_id="session:past", user_content="历史提问", assistant_content="历史回答")
    _pair(repository, suffix="002", session_id="session:current", user_content="当前提问", assistant_content="当前回答")
    _pair(repository, suffix="003", session_id="session:past", assistant_status="cancelled")
    _pair(repository, suffix="004", session_id="session:other-project", project_id="project:other")

    service = ConversationRecallService(repository)

    write_side = service.recall(query="历史", current_session_id="session:current")
    assert [item.request_id for item in write_side.episodes] == ["request:recall:001"]
    assert service.rebuild() == 2
    recall = service.recall(query="历史", current_session_id="session:current")

    assert recall.byte_count == len(recall.episodes[0].summary.encode("utf-8"))
    assert [(item.session_id, item.request_id) for item in recall.episodes] == [
        ("session:past", "request:recall:001"),
    ]
    episode = recall.episodes[0]
    assert episode.agent_id == "companion.chat"
    assert episode.user_message_id == "message:user:001"
    assert episode.assistant_message_id == "message:assistant:001"
    assert episode.summary == "用户：历史提问\n助手：历史回答"
    assert repository.get_conversation_episode(
        project_id="default", session_id="session:past", episode_id=episode.episode_id,
    ) == episode
    assert repository.get_conversation_episode(
        project_id="default", session_id="session:current", episode_id=episode.episode_id,
    ) is None


def test_rebuild_is_deterministic_and_recall_enforces_limit_and_utf8_budget(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")
    _session(repository, "session:past")
    _session(repository, "session:current")
    _pair(repository, suffix="005", session_id="session:past", user_content="甲" * 2000, assistant_content="乙" * 2000)
    _pair(repository, suffix="006", session_id="session:past", user_content="短问题", assistant_content="短回答")
    service = ConversationRecallService(repository)

    assert service.rebuild() == 2
    first = service.recall(query="短", current_session_id="session:current", limit=1)
    assert service.rebuild() == 2
    second = service.recall(query="短", current_session_id="session:current", limit=1)
    assert second == first
    assert len(first.episodes) == 1
    assert first.byte_count <= 2_400
    relevant = service.recall(query="甲", current_session_id="session:current", limit=1)
    assert relevant.episodes[0].request_id == "request:recall:005"
    constrained = service.recall(query="短", current_session_id="session:current", limit=4, byte_budget=5)
    assert constrained.episodes == ()
    assert constrained.byte_count == 0


def test_revision_drift_and_hard_forget_make_projection_unreadable(tmp_path: Path) -> None:
    database = tmp_path / "companion.sqlite3"
    repository = _repository(database)
    _session(repository, "session:past")
    _session(repository, "session:current")
    user_id, assistant_id = _pair(repository, suffix="007", session_id="session:past")
    service = ConversationRecallService(repository)

    assert service.rebuild() == 1
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE companion_messages SET revision = revision + 1 WHERE message_id = ?", (assistant_id,))
        connection.commit()
    assert service.recall(query="用户", current_session_id="session:current").episodes == ()
    assert service.rebuild() == 1
    assert len(service.recall(query="用户", current_session_id="session:current").episodes) == 1

    forgotten = CompanionHistoryService(repository).forget(user_id)

    assert forgotten.status == "completed"
    assert repository.get_message(user_id) is None
    assert repository.get_message(assistant_id) is not None
    assert service.recall(query="用户", current_session_id="session:current").episodes == ()
