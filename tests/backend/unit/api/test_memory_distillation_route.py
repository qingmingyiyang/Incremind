from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.memory_distillation import CompanionMemoryDistillationCommand, router
from core.companion_core import CompanionRepository
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core.memory_distillation import (
    MemoryDistillationError,
    ObjectStoreMemoryDistillationDiaryRepository,
)
from core.storage_provider import JsonObjectStore


NOW = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)


def _command(tmp_path):
    repository = CompanionRepository(tmp_path / "companion.sqlite3", now=lambda: NOW)
    for session_id in ("session-past", "session-current"):
        repository.create_session(
            session_id=session_id, context_epoch=1, prompt_revision=1, profile_revision=1,
            project_id="project-a", started_at="2026-08-31T08:00:00+00:00",
        )
    repository.append_message(
        message_id="message-user", request_id="request-past", session_id="session-past", context_epoch=1,
        project_id="project-a", role="user", status="completed", content="我们如何保存这个长期项目决定？",
        provider_mode="none", created_at="2026-08-01T10:00:00+00:00",
    )
    repository.append_message(
        message_id="message-assistant", request_id="request-past", session_id="session-past", context_epoch=1,
        project_id="project-a", role="assistant", status="completed", content="项目继续使用本地 SQLite 作为唯一写入权威，并保留人工审核。",
        provider_mode="local", created_at="2026-08-01T10:01:00+00:00",
    )
    episode = repository.list_conversation_episodes(
        project_id="project-a", current_session_id="session-current", limit=12,
    )[0]
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    return (
        CompanionMemoryDistillationCommand(
            repository=repository,
            candidates=ObjectStoreMemoryCandidateRepository(store),
            diary=ObjectStoreMemoryDistillationDiaryRepository(store),
            now="2026-08-31T10:00:00+00:00",
        ),
        store,
        episode.episode_id,
    )


def test_explicit_command_creates_body_free_diary_and_pending_review_candidate(tmp_path) -> None:
    command, store, episode_id = _command(tmp_path)

    result = command.execute(
        project_id="project-a", current_session_id="session-current", episode_id=episode_id,
        command_id="distill-command-001",
    )

    assert result["status"] == "pending_review"
    assert result["proposal_only"] is True
    assert result["auto_publication"] == "disabled"
    assert result["candidate_id"]
    candidate = store.read("memory_candidates", str(result["candidate_id"]))
    assert candidate is not None and candidate["status"] == "pending_review"
    assert candidate["review"]["auto_promote_allowed"] is False
    diary = store.read("memory_distillation_diary", str(result["diary"]["id"]))
    assert diary is not None
    assert diary["source_refs"]
    assert diary["reason_codes"] == ["eligible_grounded_episode", "requires_user_review"]
    assert diary["content_included"] is False
    assert "proposed_content" not in str(diary)

    replayed = command.execute(
        project_id="project-a", current_session_id="session-current", episode_id=episode_id,
        command_id="distill-command-001",
    )
    assert replayed["replayed"] is True
    assert replayed["candidate_id"] == result["candidate_id"]
    assert len(store.list("memory_candidates")) == 1


def test_explicit_command_rejects_cross_project_or_current_session_episode(tmp_path) -> None:
    command, _store, episode_id = _command(tmp_path)

    with pytest.raises(MemoryDistillationError, match="current session"):
        command.execute(
            project_id="project-b", current_session_id="session-current", episode_id=episode_id,
            command_id="distill-command-002",
        )
    with pytest.raises(MemoryDistillationError, match="unavailable"):
        command.execute(
            project_id="project-a", current_session_id="session-past", episode_id=episode_id,
            command_id="distill-command-003",
        )


def test_episode_listing_is_project_scoped_bounded_and_has_no_message_body_fields(tmp_path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path, now=lambda: NOW)
    for session_id in ("session-past", "session-current"):
        repository.create_session(
            session_id=session_id, context_epoch=1, prompt_revision=1, profile_revision=1,
            project_id="project-a", started_at="2026-08-31T08:00:00+00:00",
        )
    private_user_body = "private-user-body-must-not-be-returned"
    private_assistant_body = "private-assistant-body-must-not-be-returned"
    repository.append_message(
        message_id="message-user", request_id="request-past", session_id="session-past",
        context_epoch=1, project_id="project-a", role="user", status="completed",
        content=private_user_body, provider_mode="none", created_at="2026-08-01T10:00:00+00:00",
    )
    repository.append_message(
        message_id="message-assistant", request_id="request-past", session_id="session-past",
        context_epoch=1, project_id="project-a", role="assistant", status="completed",
        content=private_assistant_body, provider_mode="local", created_at="2026-08-01T10:01:00+00:00",
    )
    expected = repository.list_conversation_episodes(
        project_id="project-a", current_session_id="session-current", limit=12,
    )[0]
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get(
            "/api/rebuild/companion/memory-distillations/episodes",
            params={"project_id": "project-a", "current_session_id": "session-current"},
        )
        rejected = client.get(
            "/api/rebuild/companion/memory-distillations/episodes",
            params={"project_id": "project-b", "current_session_id": "session-current"},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"items": [{
        "episode_id": expected.episode_id,
        "session_id": "session-past",
        "occurred_at": "2026-08-01T10:01:00+00:00",
        "summary": expected.summary,
        "source_turns": ["message-user", "message-assistant"],
    }]}
    assert set(response.json()["items"][0]) == {
        "episode_id", "session_id", "occurred_at", "summary", "source_turns",
    }
    assert "user_message" not in response.text
    assert "assistant_message" not in response.text
    assert rejected.status_code == 409
