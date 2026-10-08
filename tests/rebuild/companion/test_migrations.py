from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.companion_core import (
    CompanionIntegrityError,
    CompanionRepository,
    CompanionSchemaTooNew,
)
from core.companion_core import schema as companion_schema
from core.companion_core import repository as companion_repository


FIXED_NOW = datetime(2026, 7, 19, 4, 0, tzinfo=timezone.utc)
EXPECTED_TABLES = {
    "companion_appearance_receipts",
    "companion_commerce_receipts",
    "companion_conversation_episodes",
    "companion_diaries",
    "companion_forget_receipts",
    "companion_focus_sessions",
    "companion_interaction_events",
    "companion_inventory",
    "companion_master_profile",
    "companion_message_dependencies",
    "companion_messages",
    "companion_migrations",
    "companion_prompt_binding",
    "companion_provider_jobs",
    "companion_random_events",
    "companion_reminders",
    "companion_reminder_occurrences",
    "companion_sessions",
    "companion_settings",
    "companion_state",
    "companion_state_actions",
    "companion_story_progress",
    "companion_unlock_events",
    "companion_wallet_ledger",
}


def _repository(path: Path) -> CompanionRepository:
    return CompanionRepository(path, now=lambda: FIXED_NOW)


def test_empty_database_migrates_to_current_schema_with_required_pragmas_and_tables(tmp_path: Path) -> None:
    database = tmp_path / "companion.sqlite3"
    repository = _repository(database)

    status = repository.initialize()

    assert status.schema_version == 11
    assert status.applied_migrations == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
    assert status.journal_mode == "wal"
    assert status.foreign_keys_enabled is True
    assert status.synchronous_mode == "full"
    assert status.busy_timeout_ms == 5_000
    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'companion_%'"
            )
        }
        ledger = connection.execute(
            "SELECT version, migration_id, checksum, applied_at FROM companion_migrations"
        ).fetchall()
        state = connection.execute(
            "SELECT affinity, mood, coins, revision FROM companion_state WHERE id = 'current'"
        ).fetchone()
    finally:
        connection.close()
    assert tables == EXPECTED_TABLES
    assert [(row[0], row[1]) for row in ledger] == [
        (1, "001_companion_authority"),
        (2, "002_companion_hard_forget_receipts"),
        (3, "003_companion_reminder_occurrences"),
            (4, "004_companion_state_actions"),
                (5, "005_companion_commerce_receipts"),
                (6, "006_companion_focus_sessions"),
                (7, "007_companion_appearance_story"),
                (8, "008_companion_message_memory_review"),
                (9, "009_companion_message_project_scope"),
                (10, "010_companion_session_project_scope"),
                (11, "011_conversation_episode_projection"),
    ]
    assert all(len(row[2]) == 64 and row[3] == "2026-07-19T04:00:00+00:00" for row in ledger)
    assert state == (0, "normal", 0, 1)


def test_migration_is_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "companion.sqlite3")

    first = repository.initialize()
    second = repository.initialize()

    assert first.applied_migrations == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
    assert second.applied_migrations == ()


def test_v0_database_is_treated_as_old_and_migrated(tmp_path: Path) -> None:
    database = tmp_path / "companion.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=0")
    connection.commit()
    connection.close()

    status = _repository(database).initialize()

    assert status.schema_version == 11
    assert status.applied_migrations == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)


def test_v7_database_adds_empty_memory_review_without_rewriting_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "companion.sqlite3"
    first_seven = companion_schema.MIGRATIONS[:7]
    monkeypatch.setattr(companion_schema, "SCHEMA_VERSION", 7)
    monkeypatch.setattr(companion_schema, "MIGRATIONS", first_seven)
    monkeypatch.setattr(companion_repository, "SCHEMA_VERSION", 7)
    old = _repository(database)
    old.initialize()
    connection = sqlite3.connect(database)
    connection.execute(
        """
        INSERT INTO companion_sessions (
            session_id, context_epoch, prompt_revision, profile_revision,
            started_at, closed_at, revision
        ) VALUES (?, ?, ?, ?, ?, NULL, 1)
        """,
        ("session:legacy", 1, 1, 1, "2026-07-19T04:00:00+00:00"),
    )
    connection.execute(
        """
        INSERT INTO companion_messages (
            message_id, request_id, session_id, context_epoch, role, status,
            content, created_at, provider_mode, revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "message:legacy", "request:legacy", "session:legacy", 1, "assistant", "completed",
            "保留的旧回复", "2026-07-19T04:01:00+00:00", "local", 1,
        ),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(companion_schema, "SCHEMA_VERSION", 11)
    monkeypatch.setattr(
        companion_schema, "MIGRATIONS",
        (*first_seven, companion_schema.MIGRATION_8, companion_schema.MIGRATION_9, companion_schema.MIGRATION_10, companion_schema.MIGRATION_11),
    )
    monkeypatch.setattr(companion_repository, "SCHEMA_VERSION", 11)

    repository = _repository(database)
    status = repository.initialize()
    message = repository.get_message("message:legacy")

    assert status.applied_migrations == (8, 9, 10, 11)
    assert message is not None
    assert message.content == "保留的旧回复"
    assert message.memory_review == {}
    assert message.project_id == "default"
    session = repository.get_session("session:legacy")
    assert session is not None and session.project_id == "default"


def test_v1_database_upgrades_without_rewriting_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "companion.sqlite3"
    monkeypatch.setattr(companion_schema, "SCHEMA_VERSION", 1)
    monkeypatch.setattr(companion_schema, "MIGRATIONS", (companion_schema.MIGRATION_1,))
    monkeypatch.setattr(companion_repository, "SCHEMA_VERSION", 1)
    _repository(database).initialize()
    monkeypatch.setattr(companion_schema, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(companion_schema, "MIGRATIONS", (companion_schema.MIGRATION_1, companion_schema.MIGRATION_2, companion_schema.MIGRATION_3))
    monkeypatch.setattr(companion_repository, "SCHEMA_VERSION", 3)

    status = _repository(database).initialize()

    assert status.applied_migrations == (2, 3)
    assert status.schema_version == 3


def test_v2_database_adds_occurrences_without_rewriting_existing_reminders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "companion.sqlite3"
    monkeypatch.setattr(companion_schema, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(companion_schema, "MIGRATIONS", (companion_schema.MIGRATION_1, companion_schema.MIGRATION_2))
    monkeypatch.setattr(companion_repository, "SCHEMA_VERSION", 2)
    _repository(database).initialize()
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO companion_reminders VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy", '{"title":"保留我"}', 5, "2026-07-20T01:00:00+00:00", "pending", 1, "2026-07-19T04:00:00+00:00"),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(companion_schema, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(companion_schema, "MIGRATIONS", (companion_schema.MIGRATION_1, companion_schema.MIGRATION_2, companion_schema.MIGRATION_3))
    monkeypatch.setattr(companion_repository, "SCHEMA_VERSION", 3)
    status = _repository(database).initialize()
    connection = sqlite3.connect(database)
    try:
        title = connection.execute("SELECT json_extract(schedule_json, '$.title') FROM companion_reminders WHERE reminder_id = 'legacy'").fetchone()[0]
        occurrence_table = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'companion_reminder_occurrences'").fetchone()
    finally:
        connection.close()
    assert status.applied_migrations == (3,)
    assert title == "保留我" and occurrence_table == (1,)


def test_v3_database_adds_state_receipts_without_rewriting_snapshot_or_wallet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "companion.sqlite3"
    first_three = (companion_schema.MIGRATION_1, companion_schema.MIGRATION_2, companion_schema.MIGRATION_3)
    monkeypatch.setattr(companion_schema, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(companion_schema, "MIGRATIONS", first_three)
    monkeypatch.setattr(companion_repository, "SCHEMA_VERSION", 3)
    old = _repository(database)
    old.initialize()
    old.record_wallet_transaction(
        transaction_id="legacy:wallet", idempotency_key="legacy:wallet", reason="旧余额", delta=7,
        created_at="2026-07-19T04:00:00+00:00",
    )
    monkeypatch.setattr(companion_schema, "SCHEMA_VERSION", 4)
    monkeypatch.setattr(companion_schema, "MIGRATIONS", (*first_three, companion_schema.MIGRATION_4))
    monkeypatch.setattr(companion_repository, "SCHEMA_VERSION", 4)
    repository = _repository(database)
    status = repository.initialize()
    assert status.applied_migrations == (4,)
    assert repository.get_state_snapshot().coins == 7
    assert repository.wallet_integrity().consistent is True
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM companion_state_actions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM companion_unlock_events").fetchone()[0] == 0
    finally:
        connection.close()


def test_future_database_version_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "companion.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=99")
    connection.commit()
    connection.close()

    with pytest.raises(CompanionSchemaTooNew):
        _repository(database).initialize()


def test_changed_migration_checksum_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "companion.sqlite3"
    _repository(database).initialize()
    connection = sqlite3.connect(database)
    connection.execute("UPDATE companion_migrations SET checksum = ? WHERE version = 1", ("0" * 64,))
    connection.commit()
    connection.close()

    with pytest.raises(CompanionIntegrityError, match="checksum"):
        _repository(database).initialize()


def test_migration_ledger_cannot_run_ahead_of_user_version(tmp_path: Path) -> None:
    database = tmp_path / "companion.sqlite3"
    _repository(database).initialize()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=0")
    connection.commit()
    connection.close()

    with pytest.raises(CompanionIntegrityError, match="same history"):
        _repository(database).initialize()


def test_failed_migration_rolls_back_all_schema_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "companion.sqlite3"
    broken = companion_schema.CompanionMigration(
        version=1,
        migration_id="001_broken",
        statements=("CREATE TABLE partial_table (id TEXT PRIMARY KEY)", "INVALID SQL"),
    )
    monkeypatch.setattr(companion_schema, "MIGRATIONS", (broken,))
    monkeypatch.setattr(companion_schema, "SCHEMA_VERSION", 1)

    with pytest.raises(sqlite3.Error):
        _repository(database).initialize()

    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    assert "partial_table" not in tables
    assert "companion_migrations" not in tables
    assert version == 0


def test_concurrent_initializers_apply_migration_once(tmp_path: Path) -> None:
    database = tmp_path / "companion.sqlite3"

    def initialize() -> tuple[int, ...]:
        return _repository(database).initialize().applied_migrations

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = tuple(executor.map(lambda _: initialize(), range(6)))

    assert results.count((1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)) == 1
    assert results.count(()) == 5
