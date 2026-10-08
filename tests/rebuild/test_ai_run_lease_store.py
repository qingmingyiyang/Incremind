from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.ai_kernel import InMemoryTurnStateStore, SQLiteAITurnStore


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path):
    return InMemoryTurnStateStore() if request.param == "memory" else SQLiteAITurnStore(tmp_path / "turns.sqlite3")


def test_strict_run_lease_lifecycle_has_identical_boundary_behavior(store) -> None:
    turn_id = _claim(store)
    token = store.try_acquire_run_lease(turn_id, "owner-a", now=NOW, stale_after=NOW + timedelta(seconds=10))
    assert token is not None and token.generation == 1
    assert store.try_acquire_run_lease(turn_id, "owner-b", now=NOW, stale_after=NOW + timedelta(seconds=10)) is None
    assert store.renew_run_lease(token, now=NOW + timedelta(seconds=2), stale_after=NOW + timedelta(seconds=12)).heartbeat_at == NOW + timedelta(seconds=2)
    assert store.mark_run_lease_stale(token, now=NOW + timedelta(seconds=11)) is None
    stale = store.mark_run_lease_stale(token, now=NOW + timedelta(seconds=12))
    assert stale is not None and stale.status == "recovery_required"
    assert store.assert_active_run_lease(token) is None
    quarantined = store.takeover_run_lease(turn_id, expected_generation=1, owner_id="owner-b", now=NOW + timedelta(seconds=13), stale_after=NOW + timedelta(seconds=23), disposition="quarantined")
    assert quarantined is not None and quarantined.status == "quarantined" and quarantined.token.generation == 1
    assert store.takeover_run_lease(turn_id, expected_generation=1, owner_id="owner-b", now=NOW + timedelta(seconds=13), stale_after=NOW + timedelta(seconds=23), disposition="safe") is None


def test_safe_takeover_fences_old_token_and_old_release(store) -> None:
    turn_id = _claim(store)
    first = store.try_acquire_run_lease(turn_id, "owner-a", now=NOW, stale_after=NOW + timedelta(seconds=1))
    assert first is not None
    assert store.mark_run_lease_stale(first, now=NOW + timedelta(seconds=1)) is not None
    assert store.takeover_run_lease(turn_id, expected_generation=1, owner_id="owner-b", now=NOW, stale_after=NOW + timedelta(seconds=10), disposition="safe") is None
    replacement = store.takeover_run_lease(turn_id, expected_generation=1, owner_id="owner-b", now=NOW + timedelta(seconds=2), stale_after=NOW + timedelta(seconds=12), disposition="safe")
    assert replacement is not None and replacement.status == "active" and replacement.token.generation == 2
    assert store.assert_active_run_lease(first) is None
    store.release_strict_run_lease(first)
    assert store.assert_active_run_lease(replacement.token) is not None
    assert store.renew_run_lease(first, now=NOW + timedelta(seconds=3), stale_after=NOW + timedelta(seconds=13)) is None
    assert store.takeover_run_lease(turn_id, expected_generation=1, owner_id="owner-c", now=NOW + timedelta(seconds=3), stale_after=NOW + timedelta(seconds=13), disposition="safe") is None


def test_strict_lease_rejects_non_utc_and_invalid_time_range(store) -> None:
    turn_id = _claim(store)
    with pytest.raises(ValueError):
        store.try_acquire_run_lease(turn_id, "owner-a", now=datetime(2026, 8, 24, 8, 0), stale_after=NOW)
    with pytest.raises(ValueError):
        store.try_acquire_run_lease(turn_id, "owner-a", now=NOW, stale_after=NOW - timedelta(seconds=1))


def test_legacy_and_strict_lease_authority_are_mutually_exclusive(store) -> None:
    turn_id = _claim(store)
    strict = store.try_acquire_run_lease(turn_id, "owner-a", now=NOW, stale_after=NOW + timedelta(seconds=10))
    assert strict is not None
    assert store.try_claim_run_lease(turn_id, "legacy-owner") is None
    store.release_strict_run_lease(strict)
    assert store.try_claim_run_lease(turn_id, "legacy-owner") == 2


def test_strict_acquire_rejects_existing_legacy_lease(store) -> None:
    turn_id = _claim(store)
    assert store.try_claim_run_lease(turn_id, "legacy-owner") == 1
    assert store.try_acquire_run_lease(turn_id, "owner-a", now=NOW, stale_after=NOW + timedelta(seconds=10)) is None


def test_recovery_or_quarantine_cannot_be_released_by_old_token(store) -> None:
    turn_id = _claim(store)
    token = store.try_acquire_run_lease(turn_id, "owner-a", now=NOW, stale_after=NOW + timedelta(seconds=1))
    assert token is not None
    assert store.mark_run_lease_stale(token, now=NOW + timedelta(seconds=1)) is not None
    store.release_strict_run_lease(token)
    replacement = store.takeover_run_lease(turn_id, expected_generation=1, owner_id="owner-b", now=NOW + timedelta(seconds=2), stale_after=NOW + timedelta(seconds=12), disposition="safe")
    assert replacement is not None and replacement.token.generation == 2
    assert store.mark_run_lease_stale(replacement.token, now=NOW + timedelta(seconds=12)) is not None
    quarantined = store.takeover_run_lease(turn_id, expected_generation=2, owner_id="owner-c", now=NOW + timedelta(seconds=13), stale_after=NOW + timedelta(seconds=23), disposition="quarantined")
    assert quarantined is not None and quarantined.status == "quarantined"
    store.release_strict_run_lease(replacement.token)
    assert store.try_acquire_run_lease(turn_id, "owner-c", now=NOW + timedelta(seconds=14), stale_after=NOW + timedelta(seconds=24)) is None


def test_renew_rejects_heartbeat_clock_rollback(store) -> None:
    turn_id = _claim(store)
    token = store.try_acquire_run_lease(turn_id, "owner-a", now=NOW, stale_after=NOW + timedelta(seconds=10))
    assert token is not None
    updated = store.renew_run_lease(token, now=NOW + timedelta(seconds=3), stale_after=NOW + timedelta(seconds=13))
    assert updated is not None
    assert store.renew_run_lease(token, now=NOW + timedelta(seconds=2), stale_after=NOW + timedelta(seconds=12)) is None
    assert store.assert_active_run_lease(token).heartbeat_at == NOW + timedelta(seconds=3)


def test_sqlite_strict_lease_is_visible_to_legacy_table_readers_and_writers(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store = SQLiteAITurnStore(database)
    turn_id = _claim(store)
    token = store.try_acquire_run_lease(turn_id, "owner-a", now=NOW, stale_after=NOW + timedelta(seconds=10))
    assert token is not None
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT owner_id, generation FROM ai_turn_run_leases WHERE turn_id=?", (turn_id,)).fetchone()
        assert row == ("owner-a", 1)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO ai_turn_run_leases(turn_id, owner_id, generation) VALUES(?,?,?)", (turn_id, "legacy-owner", 2))
    finally:
        connection.close()


def test_sqlite_old_three_column_lease_insert_blocks_strict_acquire(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store = SQLiteAITurnStore(database)
    turn_id = _claim(store)
    connection = sqlite3.connect(database)
    try:
        connection.execute("INSERT INTO ai_turn_run_leases(turn_id, owner_id, generation) VALUES(?,?,?)", (turn_id, "legacy-owner", 1))
        connection.commit()
    finally:
        connection.close()
    assert store.try_acquire_run_lease(turn_id, "owner-a", now=NOW, stale_after=NOW + timedelta(seconds=10)) is None


def test_sqlite_historical_strict_lease_migration_is_atomic_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "historical.sqlite3"
    _create_historical_lease_database(database, conflict=False)
    first = SQLiteAITurnStore(database)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_turn_strict_run_leases'").fetchone() is None
        assert connection.execute("SELECT owner_id, generation, status FROM ai_turn_run_leases").fetchall() == [("strict-owner", 7, "active")]
    finally:
        connection.close()
    second = SQLiteAITurnStore(database)
    assert first.try_claim_run_lease("turn-historical", "legacy-owner") is None
    assert second.try_acquire_run_lease("turn-historical", "new-owner", now=NOW, stale_after=NOW + timedelta(seconds=10)) is None


def test_sqlite_historical_dual_authority_conflict_rolls_back_without_data_loss(tmp_path: Path) -> None:
    database = tmp_path / "conflict.sqlite3"
    _create_historical_lease_database(database, conflict=True)
    with pytest.raises(RuntimeError, match="incompatible historical AI Turn lease authorities"):
        SQLiteAITurnStore(database)
    connection = sqlite3.connect(database)
    try:
        assert [row[1] for row in connection.execute("PRAGMA table_info(ai_turn_run_leases)")] == ["turn_id", "owner_id", "generation"]
        assert connection.execute("SELECT owner_id, generation FROM ai_turn_run_leases").fetchall() == [("legacy-owner", 3)]
        assert connection.execute("SELECT owner_id, generation FROM ai_turn_strict_run_leases").fetchall() == [("strict-owner", 4)]
    finally:
        connection.close()


def _create_historical_lease_database(path: Path, *, conflict: bool) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE ai_turn_run_leases(turn_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, generation INTEGER NOT NULL)")
        connection.execute("CREATE TABLE ai_turn_strict_run_leases(turn_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, generation INTEGER NOT NULL, status TEXT NOT NULL, acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, stale_after TEXT NOT NULL)")
        if conflict:
            connection.execute("INSERT INTO ai_turn_run_leases VALUES(?,?,?)", ("turn-historical", "legacy-owner", 3))
            connection.execute("INSERT INTO ai_turn_strict_run_leases VALUES(?,?,?,?,?,?,?)", ("turn-historical", "strict-owner", 4, "active", NOW.isoformat(), NOW.isoformat(), (NOW + timedelta(seconds=10)).isoformat()))
        else:
            connection.execute("INSERT INTO ai_turn_strict_run_leases VALUES(?,?,?,?,?,?,?)", ("turn-historical", "strict-owner", 7, "active", NOW.isoformat(), NOW.isoformat(), (NOW + timedelta(seconds=10)).isoformat()))
        connection.commit()
    finally:
        connection.close()


def _claim(store) -> str:
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    return store.claim_turn(request)[0]
