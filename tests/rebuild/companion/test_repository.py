from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.companion_core import (
    CompanionConflict,
    CompanionRepository,
    CompanionRepositoryError,
)


FIXED_NOW = datetime(2026, 7, 19, 5, 0, tzinfo=timezone.utc)


def _repository(path: Path) -> CompanionRepository:
    return CompanionRepository(path, now=lambda: FIXED_NOW)


def _session(repository: CompanionRepository, session_id: str = "session:001") -> None:
    repository.create_session(
        session_id=session_id,
        context_epoch=1,
        prompt_revision=1,
        profile_revision=1,
        started_at="2026-07-19T05:00:00+00:00",
    )


def test_default_data_root_is_scoped_to_companion_database(tmp_path: Path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path, now=lambda: FIXED_NOW)

    repository.initialize()

    assert repository.database_path == tmp_path / ".rebuild-data" / "companion" / "companion.sqlite3"
    assert repository.database_path.is_file()


def test_session_create_is_idempotent_and_conflicting_reuse_is_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")

    first = repository.create_session(
        session_id="session:001",
        context_epoch=1,
        prompt_revision=2,
        profile_revision=3,
        started_at="2026-07-19T05:00:00+00:00",
    )
    replay = repository.create_session(
        session_id="session:001",
        context_epoch=1,
        prompt_revision=2,
        profile_revision=3,
        started_at="2026-07-19T05:00:00+00:00",
    )

    assert replay == first
    assert first.project_id == "default"
    with pytest.raises(CompanionConflict):
        repository.create_session(
            session_id="session:001",
            context_epoch=2,
            prompt_revision=2,
            profile_revision=3,
            started_at="2026-07-19T05:00:00+00:00",
        )


def test_session_project_scope_is_persisted_and_immutable(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")
    created = repository.create_session(
        session_id="session:project", context_epoch=1, prompt_revision=1,
        profile_revision=1, started_at="2026-07-19T05:00:00+00:00",
        project_id="品牌 项目",
    )
    restarted = _repository(tmp_path / "companion.sqlite3").get_session(created.session_id)

    assert restarted is not None and restarted.project_id == "品牌 项目"
    with pytest.raises(CompanionConflict, match="different input"):
        repository.create_session(
            session_id="session:project", context_epoch=1, prompt_revision=1,
            profile_revision=1, started_at="2026-07-19T05:00:00+00:00",
            project_id="另一个项目",
        )


def test_message_requires_existing_session_and_replay_is_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")
    repository.initialize()

    with pytest.raises(CompanionConflict, match="session"):
        repository.append_message(
            message_id="message:missing-session",
            request_id="request:missing-session",
            session_id="session:missing",
            context_epoch=1,
            role="user",
            status="completed",
            content="hello",
            created_at="2026-07-19T05:01:00+00:00",
            provider_mode="none",
        )

    _session(repository)
    first = repository.append_message(
        message_id="message:user:001",
        request_id="request:chat:001",
        session_id="session:001",
        context_epoch=1,
        role="user",
        status="completed",
        content="你好。",
        created_at="2026-07-19T05:01:00+00:00",
        provider_mode="none",
    )
    replay = repository.append_message(
        message_id="message:user:001",
        request_id="request:chat:001",
        session_id="session:001",
        context_epoch=1,
        role="user",
        status="completed",
        content="你好。",
        created_at="2026-07-19T05:01:00+00:00",
        provider_mode="none",
    )

    assert replay == first
    assert first.project_id == "default"
    with pytest.raises(CompanionConflict, match="different input"):
        repository.append_message(
            message_id="message:user:001",
            request_id="request:chat:001",
            session_id="session:001",
            context_epoch=1,
            role="user",
            status="completed",
            content="不同内容",
            created_at="2026-07-19T05:01:00+00:00",
            provider_mode="none",
        )

    scoped = repository.append_message(
        message_id="message:user:project", request_id="request:chat:project",
        session_id="session:001", context_epoch=1, role="user", status="completed",
        content="项目事实", created_at="2026-07-19T05:02:00+00:00", provider_mode="none",
        project_id="品牌 项目",
    )
    restarted = _repository(tmp_path / "companion.sqlite3").get_message(scoped.message_id)
    assert restarted is not None and restarted.project_id == "品牌 项目"
    assert [item.message_id for item in repository.list_context_messages(
        session_id="session:001", context_epoch=1, project_id="品牌 项目",
    )] == [scoped.message_id]
    with pytest.raises(CompanionConflict, match="different input"):
        repository.append_message(
            message_id=scoped.message_id, request_id="request:chat:project",
            session_id="session:001", context_epoch=1, role="user", status="completed",
            content="项目事实", created_at="2026-07-19T05:02:00+00:00", provider_mode="none",
            project_id="另一个项目",
        )


def test_failed_message_placeholder_cannot_persist_content(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")
    _session(repository)

    with pytest.raises(CompanionRepositoryError, match="must not persist content"):
        repository.append_message(
            message_id="message:assistant:failed",
            request_id="request:chat:failed",
            session_id="session:001",
            context_epoch=1,
            role="assistant",
            status="failed",
            content="provider secret or partial reply",
            created_at="2026-07-19T05:02:00+00:00",
            provider_mode="remote",
        )


def test_message_persists_only_bounded_memory_review_metadata(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")
    _session(repository)
    review = {
        "review_scope": "today_memory_review", "status": "recalled",
        "matched_count": 2, "memory_ids": ["atom-one", "scenario:two"], "generated": True,
    }

    created = repository.append_message(
        message_id="message:assistant:review", request_id="request:review", session_id="session:001",
        context_epoch=1, role="assistant", status="completed", content="今日回顾",
        created_at="2026-07-19T05:02:00+00:00", provider_mode="remote", memory_review=review,
    )
    restarted = _repository(tmp_path / "companion.sqlite3").get_message("message:assistant:review")

    assert created.memory_review == review
    assert restarted is not None and restarted.memory_review == review
    conversation_review = {
        **review, "review_scope": "conversation_context", "matched_count": 1,
        "memory_ids": ["atom-conversation"],
    }
    conversation = repository.append_message(
        message_id="message:assistant:conversation", request_id="request:conversation", session_id="session:001",
        context_epoch=1, role="assistant", status="completed", content="项目上下文回复",
        created_at="2026-07-19T05:02:30+00:00", provider_mode="remote", memory_review=conversation_review,
    )
    assert conversation.memory_review == conversation_review
    assert _repository(tmp_path / "companion.sqlite3").get_message(
        "message:assistant:conversation",
    ).memory_review == conversation_review
    with pytest.raises(CompanionRepositoryError, match="evidence"):
        repository.append_message(
            message_id="message:assistant:invalid", request_id="request:invalid", session_id="session:001",
            context_epoch=1, role="assistant", status="completed", content="不应保存",
            created_at="2026-07-19T05:03:00+00:00", provider_mode="remote",
            memory_review={**review, "memory_ids": ["../private", "原文秘密"]},
        )


def test_message_timeline_uses_stable_opaque_cursor_without_duplicates(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")
    _session(repository)
    for index in range(5):
        repository.append_message(
            message_id=f"message:user:{index:03d}",
            request_id=f"request:chat:{index:03d}",
            session_id="session:001",
            context_epoch=1,
            role="user",
            status="completed",
            content=f"message {index}",
            created_at=f"2026-07-19T05:0{index}:00+00:00",
            provider_mode="none",
        )

    first = repository.list_messages(limit=2)
    second = repository.list_messages(limit=2, before=first.next_cursor)
    third = repository.list_messages(limit=2, before=second.next_cursor)

    ids = [item.message_id for page in (first, second, third) for item in page.items]
    assert ids == [
        "message:user:004",
        "message:user:003",
        "message:user:002",
        "message:user:001",
        "message:user:000",
    ]
    assert len(ids) == len(set(ids))
    assert third.next_cursor is None
    with pytest.raises(CompanionRepositoryError, match="cursor"):
        repository.list_messages(before="not-a-valid-cursor")


def test_wallet_ledger_is_idempotent_and_recomputes_snapshot(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")

    earned = repository.record_wallet_transaction(
        transaction_id="transaction:focus:001",
        idempotency_key="focus:session:001:complete",
        reason="focus completed",
        delta=12,
        created_at="2026-07-19T05:10:00+00:00",
    )
    replay = repository.record_wallet_transaction(
        transaction_id="transaction:focus:001",
        idempotency_key="focus:session:001:complete",
        reason="focus completed",
        delta=12,
        created_at="2026-07-19T05:10:00+00:00",
    )

    assert earned.balance_after == 12
    assert earned.replayed is False
    assert replay.balance_after == 12
    assert replay.replayed is True
    assert repository.wallet_integrity().consistent is True
    assert repository.wallet_integrity().ledger_balance == 12

    with pytest.raises(CompanionConflict, match="different input"):
        repository.record_wallet_transaction(
            transaction_id="transaction:focus:001",
            idempotency_key="focus:session:001:complete",
            reason="focus completed",
            delta=99,
            created_at="2026-07-19T05:10:00+00:00",
        )
    with pytest.raises(CompanionConflict, match="negative"):
        repository.record_wallet_transaction(
            transaction_id="transaction:shop:001",
            idempotency_key="shop:purchase:001",
            reason="shop purchase",
            delta=-13,
            created_at="2026-07-19T05:11:00+00:00",
        )


def test_wallet_transaction_rolls_back_if_failure_occurs_after_ledger_insert(tmp_path: Path) -> None:
    class FailingRepository(CompanionRepository):
        def _after_wallet_ledger_insert(self, connection) -> None:
            raise RuntimeError("simulated power loss")

    database = tmp_path / "companion.sqlite3"
    repository = FailingRepository(database, now=lambda: FIXED_NOW)

    with pytest.raises(RuntimeError, match="simulated power loss"):
        repository.record_wallet_transaction(
            transaction_id="transaction:crash:001",
            idempotency_key="focus:crash:001",
            reason="crash simulation",
            delta=5,
            created_at="2026-07-19T05:12:00+00:00",
        )

    clean = _repository(database)
    integrity = clean.wallet_integrity()
    assert integrity.snapshot_balance == 0
    assert integrity.ledger_balance == 0
    assert integrity.consistent is True


def test_separate_repository_instances_serialize_concurrent_wallet_writes(tmp_path: Path) -> None:
    database = tmp_path / "companion.sqlite3"
    _repository(database).initialize()

    def earn(index: int) -> int:
        mutation = _repository(database).record_wallet_transaction(
            transaction_id=f"transaction:concurrent:{index:03d}",
            idempotency_key=f"focus:concurrent:{index:03d}",
            reason="concurrent focus reward",
            delta=1,
            created_at=f"2026-07-19T05:20:{index:02d}+00:00",
        )
        return mutation.balance_after

    with ThreadPoolExecutor(max_workers=8) as executor:
        balances = tuple(executor.map(earn, range(8)))

    assert sorted(balances) == list(range(1, 9))
    integrity = _repository(database).wallet_integrity()
    assert integrity.snapshot_balance == 8
    assert integrity.ledger_balance == 8
    assert integrity.consistent is True


def test_inventory_requires_compare_and_swap_revision(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")

    created = repository.set_inventory_quantity(
        item_id="food:cookie",
        quantity=2,
        expected_revision=0,
        updated_at="2026-07-19T05:30:00+00:00",
    )
    updated = repository.set_inventory_quantity(
        item_id="food:cookie",
        quantity=1,
        expected_revision=created.revision,
        updated_at="2026-07-19T05:31:00+00:00",
    )

    assert updated.revision == 2
    assert repository.get_inventory_item("food:cookie") == updated
    with pytest.raises(CompanionConflict, match="expected revision"):
        repository.set_inventory_quantity(
            item_id="food:cookie",
            quantity=0,
            expected_revision=1,
            updated_at="2026-07-19T05:32:00+00:00",
        )


def test_repository_rejects_non_utc_time(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")

    with pytest.raises(CompanionRepositoryError, match="UTC"):
        repository.create_session(
            session_id="session:local-time",
            context_epoch=1,
            prompt_revision=1,
            profile_revision=1,
            started_at="2026-07-19T13:00:00+08:00",
        )
