from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.job_runner.history_governance import (
    GovernanceAction,
    HistoryCategory,
    HistoryGovernanceDisplay,
    build_history_governance_dry_run,
    plan_explicit_rebuild,
    scan_vault_history_metadata,
)


def _temporary_vault(tmp_path: Path) -> Path:
    database = tmp_path / "temporary-vault.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE legacy_job_history("
            "job_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,"
            "legacy_revision INTEGER NOT NULL,imported_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE effect("
            "operation_id TEXT PRIMARY KEY,kind TEXT,state TEXT NOT NULL,"
            "contract_version TEXT,result_ref TEXT)"
        )
        connection.executemany(
            "INSERT INTO legacy_job_history VALUES(?,?,?,?)",
            (
                ("legacy-completed", '{"status":"completed","body":"not read"}', 1, "now"),
                ("legacy-running", '{"status":"running","body":"not read"}', 1, "now"),
                ("legacy-failed", '{"status":"failed","body":"not read"}', 1, "now"),
            ),
        )
        connection.executemany(
            "INSERT INTO effect VALUES(?,?,?,?,?)",
            (
                ("v2-active", "document", "INFLIGHT", "effect-v2", None),
                ("v2-unknown", "cloud-asr", "UNKNOWN", "effect-v2", None),
                ("v2-failed", "document", "SETTLED_ERR", "effect-v2", None),
                ("v2-success", "document", "SETTLED_OK", "effect-v2", "receipt:one"),
            ),
        )
    return database


def test_temporary_vault_classifies_metadata_without_reading_bodies_or_writing(tmp_path: Path) -> None:
    database = _temporary_vault(tmp_path)
    inventory = scan_vault_history_metadata(database)
    by_identity = {item.identity: item for item in inventory}

    assert by_identity["legacy-completed"].category is HistoryCategory.LEGACY
    assert by_identity["legacy-running"].category is HistoryCategory.UNKNOWN
    assert by_identity["legacy-failed"].category is HistoryCategory.FAILED
    assert by_identity["v2-active"].category is HistoryCategory.EFFECT_V2
    assert by_identity["v2-unknown"].category is HistoryCategory.UNKNOWN
    assert by_identity["v2-failed"].category is HistoryCategory.FAILED
    assert by_identity["v2-success"].category is HistoryCategory.VERIFIED_SUCCESS
    assert all("body" not in repr(item) for item in inventory)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 4


def test_dry_run_never_auto_resends_unknown_and_duplicate_scans_are_stable(tmp_path: Path) -> None:
    database = _temporary_vault(tmp_path)
    first = scan_vault_history_metadata(database)
    second = scan_vault_history_metadata(database)
    dry_run = build_history_governance_dry_run(first)

    assert first == second
    unknown = next(item for item in dry_run.items if item.identity == "v2-unknown")
    assert unknown.action is GovernanceAction.RETAIN_UNKNOWN_NO_RESEND
    assert unknown.auto_resend is False
    assert dry_run.counts[HistoryCategory.UNKNOWN] == 2


def test_interruption_restart_keeps_unknown_readonly_until_an_explicit_new_admission(tmp_path: Path) -> None:
    database = _temporary_vault(tmp_path)
    before_restart = {item.identity: item for item in scan_vault_history_metadata(database)}
    after_restart = {item.identity: item for item in scan_vault_history_metadata(database)}
    interrupted = after_restart["v2-unknown"]

    assert before_restart == after_restart
    link = plan_explicit_rebuild(
        interrupted,
        new_request_identity="admission:cloud-asr:rebuild-20260905",
        reason="user reviewed unresolved upstream outcome",
    )
    assert link.old_identity == "v2-unknown"
    assert link.new_request_identity != link.old_identity
    assert link.action == "require-explicit-new-admission"
    with pytest.raises(ValueError, match="distinct new request identity"):
        plan_explicit_rebuild(interrupted, new_request_identity="v2-unknown", reason="retry")


def test_display_classification_rollback_is_reversible_and_source_database_stays_unchanged(tmp_path: Path) -> None:
    database = _temporary_vault(tmp_path)
    inventory = scan_vault_history_metadata(database)
    display = HistoryGovernanceDisplay(inventory)
    dry_run = build_history_governance_dry_run(inventory)

    assert display.apply(dry_run) == dry_run.items
    assert display.set_display_category(
        source="legacy_job_history", identity="legacy-running", category=HistoryCategory.FAILED,
    ).category is HistoryCategory.FAILED
    assert display.rollback() == inventory
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM legacy_job_history").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 4
