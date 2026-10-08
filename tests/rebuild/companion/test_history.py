from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from core.companion_core import (
    CompanionHardForgetError,
    CompanionHistoryService,
    CompanionRepository,
)


NOW = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat()


def repository(tmp_path) -> CompanionRepository:
    result = CompanionRepository.at_data_root(tmp_path, now=lambda: NOW)
    result.initialize()
    result.create_session(
        session_id="session:history", context_epoch=1, prompt_revision=1,
        profile_revision=1, started_at=STAMP,
    )
    return result


def add_message(repo: CompanionRepository, message_id: str, request_id: str, content: str, *, role: str = "user"):
    return repo.append_message(
        message_id=message_id, request_id=request_id, session_id="session:history",
        context_epoch=1, role=role, status="completed", content=content,
        created_at=STAMP, provider_mode="local",
    )


def test_history_is_stably_paginated_and_projects_dependency_state(tmp_path) -> None:
    repo = repository(tmp_path)
    add_message(repo, "message:a", "request:a", "较早")
    add_message(repo, "message:b", "request:b", "较新")
    repo.register_message_dependency(
        message_id="message:b", dependent_kind="published_memory",
        dependent_id="memory:one", created_at=STAMP,
    )

    first = repo.list_history(limit=1)
    second = repo.list_history(limit=1, before=first.next_cursor)

    assert [item.message.message_id for item in first.items] == ["message:b"]
    assert first.items[0].dependency_count == 1
    assert first.items[0].dependency_kinds == ("published_memory",)
    assert [item.message.message_id for item in second.items] == ["message:a"]
    assert second.next_cursor is None


def test_history_filters_project_before_pagination(tmp_path) -> None:
    repo = repository(tmp_path)
    repo.create_session(
        session_id="session:other", context_epoch=1, prompt_revision=1,
        profile_revision=1, started_at=STAMP, project_id="project-beta",
    )
    add_message(repo, "message:alpha-a", "request:alpha-a", "Alpha 较早")
    repo.append_message(
        message_id="message:beta", request_id="request:beta", session_id="session:other",
        context_epoch=1, role="user", status="completed", content="Beta 插入消息",
        created_at=STAMP, provider_mode="local", project_id="project-beta",
    )
    add_message(repo, "message:alpha-z", "request:alpha-z", "Alpha 较新")

    first = repo.list_history(limit=1, project_id="default")
    second = repo.list_history(limit=1, before=first.next_cursor, project_id="default")

    assert [item.message.message_id for item in first.items] == ["message:alpha-z"]
    assert [item.message.message_id for item in second.items] == ["message:alpha-a"]
    assert second.next_cursor is None
    assert {item.message.project_id for item in first.items + second.items} == {"default"}


def test_forget_physically_deletes_message_rebases_context_and_replays_receipt(tmp_path) -> None:
    repo = repository(tmp_path)
    canary = "CANARY-physical-forget-唯一内容"
    add_message(repo, "message:canary", "request:canary", canary)
    repo.record_interaction(event_id="chat:e81037afb929d04a99e10db5d1311153", kind="chat")
    before = repo.get_session("session:history")

    first = CompanionHistoryService(repo).forget("message:canary")
    replay = CompanionHistoryService(CompanionRepository.at_data_root(tmp_path, now=lambda: NOW)).forget("message:canary")

    assert first.status == "completed" and first.affected == {"message": 1}
    assert replay.receipt_id == first.receipt_id and replay.replayed is True
    assert repo.list_history().items == ()
    assert repo.list_context_messages(session_id="session:history", context_epoch=1) == ()
    assert repo.get_session("session:history").context_epoch == before.context_epoch + 1
    connection = sqlite3.connect(repo.database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM companion_messages WHERE content LIKE ?", (f"%{canary}%",)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM companion_interaction_events").fetchone()[0] == 0
        receipt = connection.execute("SELECT affected_json, failed_step FROM companion_forget_receipts").fetchone()
        assert canary not in str(receipt)
    finally:
        connection.close()
    for artifact in (repo.database_path, repo.database_path.with_name(repo.database_path.name + "-wal")):
        if artifact.exists():
            assert canary.encode("utf-8") not in artifact.read_bytes()


def test_dependency_failure_keeps_message_and_retry_resumes_without_false_success(tmp_path) -> None:
    repo = repository(tmp_path)
    add_message(repo, "message:dependent", "request:dependent", "必须保留到依赖成功")
    repo.register_message_dependency(
        message_id="message:dependent", dependent_kind="fts", dependent_id="index:one", created_at=STAMP,
    )
    calls = []

    def fail_once(dependent_id: str) -> None:
        calls.append(dependent_id)
        if len(calls) == 1:
            raise RuntimeError("index unavailable")

    service = CompanionHistoryService(repo, dependency_erasers={"fts": fail_once})
    with pytest.raises(CompanionHardForgetError) as captured:
        service.forget("message:dependent")
    assert captured.value.receipt.status == "failed"
    assert captured.value.receipt.failed_step == "erase:fts"
    assert repo.list_history().items[0].message.content == "必须保留到依赖成功"

    completed = service.forget("message:dependent")

    assert completed.status == "completed"
    assert completed.attempts == 2
    assert completed.affected == {"fts": 1, "message": 1}
    assert repo.list_history().items == ()


def test_published_memory_must_use_withdraw_adapter_before_delete(tmp_path) -> None:
    repo = repository(tmp_path)
    add_message(repo, "message:published", "request:published", "已发布来源")
    repo.register_message_dependency(
        message_id="message:published", dependent_kind="published_memory",
        dependent_id="memory:published", created_at=STAMP,
    )
    withdrawn = []

    receipt = CompanionHistoryService(
        repo, dependency_erasers={"published_memory": withdrawn.append},
    ).forget("message:published")

    assert withdrawn == ["memory:published"]
    assert receipt.affected == {"message": 1, "published_memory": 1}


def test_missing_dependency_eraser_fails_closed(tmp_path) -> None:
    repo = repository(tmp_path)
    add_message(repo, "message:vector", "request:vector", "向量来源")
    repo.register_message_dependency(
        message_id="message:vector", dependent_kind="vector", dependent_id="vector:one", created_at=STAMP,
    )

    with pytest.raises(CompanionHardForgetError) as captured:
        CompanionHistoryService(repo).forget("message:vector")

    assert captured.value.receipt.status == "failed"
    assert repo.list_history().items[0].message.message_id == "message:vector"


def test_secure_purge_failure_is_retryable_after_message_transaction_commits(tmp_path) -> None:
    class FailPurgeOnce(CompanionRepository):
        failures = 1

        def _purge_deleted_pages(self) -> None:
            if self.failures:
                self.failures -= 1
                raise sqlite3.OperationalError("busy")
            super()._purge_deleted_pages()

    base = repository(tmp_path)
    add_message(base, "message:purge", "request:purge", "WAL-CANARY")
    repo = FailPurgeOnce(base.database_path, now=lambda: NOW)

    with pytest.raises(CompanionHardForgetError) as captured:
        CompanionHistoryService(repo).forget("message:purge")

    assert captured.value.receipt.status == "failed"
    assert captured.value.receipt.failed_step == "purge:sqlite"
    assert base.list_history().items == ()
    assert [item.message_id for item in base.list_forget_recoveries()] == ["message:purge"]
    completed = CompanionHistoryService(repo).forget("message:purge")
    assert completed.status == "completed" and completed.attempts == 2
    assert base.list_forget_recoveries() == ()


def test_forget_recovery_is_visible_only_in_its_session_project(tmp_path) -> None:
    class FailPurge(CompanionRepository):
        def _purge_deleted_pages(self) -> None:
            raise sqlite3.OperationalError("busy")

    base = repository(tmp_path)
    base.create_session(
        session_id="session:beta", context_epoch=1, prompt_revision=1,
        profile_revision=1, started_at=STAMP, project_id="project-beta",
    )
    base.append_message(
        message_id="message:beta-purge", request_id="request:beta-purge",
        session_id="session:beta", context_epoch=1, role="user", status="completed",
        content="Beta purge", created_at=STAMP, provider_mode="local", project_id="project-beta",
    )

    with pytest.raises(CompanionHardForgetError):
        CompanionHistoryService(FailPurge(base.database_path, now=lambda: NOW)).forget("message:beta-purge")

    assert base.list_forget_recoveries(project_id="default") == ()
    assert [item.message_id for item in base.list_forget_recoveries(project_id="project-beta")] == [
        "message:beta-purge",
    ]


def test_repository_connections_enable_sqlite_secure_delete(tmp_path) -> None:
    repo = repository(tmp_path)
    connection = repo._open_connection()
    try:
        assert int(connection.execute("PRAGMA secure_delete").fetchone()[0]) == 1
    finally:
        connection.close()
