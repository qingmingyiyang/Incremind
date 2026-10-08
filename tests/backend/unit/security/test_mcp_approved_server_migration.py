from __future__ import annotations

import json
import multiprocessing
import sqlite3
import sys
from pathlib import Path

import pytest
from blake3 import blake3

from backend.security.mcp_approved_server_migration import (
    MCPApprovedServerMigrationAuthority,
    MCPApprovedServerMigrationConflict,
)
from backend.security.mcp_approved_servers import (
    JsonMCPApprovedServerStore,
    MCPApprovedServerStoreError,
    canonical_mcp_approved_server_payload,
)


def _process_preview(root: str, candidate: dict[str, object], command_id: str, ready, start, output) -> None:
    ready.put("ready")
    start.wait(10)
    try:
        result = MCPApprovedServerMigrationAuthority(Path(root)).preview(
            candidate=candidate, command_id=command_id
        )
        output.put(("ok", result.migration_id, result.revision, result.state))
    except Exception as error:
        output.put(("error", type(error).__name__, str(error)))


def _process_begin(root: str, migration_id: str, revision: int, command_id: str, ready, start, output) -> None:
    ready.put("ready")
    start.wait(10)
    try:
        result = MCPApprovedServerMigrationAuthority(Path(root)).begin_cutover(
            migration_id=migration_id, expected_revision=revision, command_id=command_id
        )
        output.put(("ok", result.migration_id, result.revision, result.state))
    except Exception as error:
        output.put(("error", type(error).__name__, str(error)))


def _run_process_race(ctx, targets: list[tuple[object, tuple[object, ...]]]) -> list[tuple[object, ...]]:
    ready = ctx.Queue()
    output = ctx.Queue()
    start = ctx.Event()
    processes = [ctx.Process(target=target, args=(*args, ready, start, output)) for target, args in targets]
    for process in processes:
        process.start()
    for _process in processes:
        assert ready.get(timeout=15) == "ready"
    start.set()
    results = [output.get(timeout=20) for _process in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    return results


def test_bootstrap_preview_confirm_cutover_and_explicit_rollback_keep_one_pointer(tmp_path: Path) -> None:
    _write_legacy(tmp_path, _candidate("calendar-v1", 1))
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    assert authority.active_snapshot().servers[0].host_connection.endpoint_identity == "calendar-v1"

    preview = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview-1")
    assert preview.state == "previewed"
    assert preview.active_snapshot_revision == 1
    assert preview.affected_server_count == 1
    assert authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview-1") == preview

    confirmed = authority.confirm(migration_id=preview.migration_id, expected_revision=1, command_id="confirm-1", confirmed=True)
    fence = authority.begin_cutover(migration_id=preview.migration_id, expected_revision=confirmed.revision, command_id="cutover-1")
    # The new profile cannot be active while runtime revocation is still pending.
    assert fence.state == "cutover_started" and fence.active_snapshot_revision == 1
    revoked = authority.mark_old_revoked(migration_id=preview.migration_id, expected_revision=fence.revision, command_id="revoke-old-1")
    committed = authority.commit_cutover(migration_id=preview.migration_id, expected_revision=revoked.revision, command_id="commit-1")
    assert committed.state == "cutover_committed" and committed.active_snapshot_revision == 2
    assert JsonMCPApprovedServerStore(tmp_path).snapshot().servers[0].host_connection.endpoint_identity == "calendar-v2"

    rolling = authority.begin_rollback(migration_id=preview.migration_id, expected_revision=committed.revision, command_id="rollback-1")
    new_revoked = authority.mark_new_revoked(migration_id=preview.migration_id, expected_revision=rolling.revision, command_id="revoke-new-1")
    rolled_back = authority.commit_rollback(migration_id=preview.migration_id, expected_revision=new_revoked.revision, command_id="commit-rollback-1")
    assert rolled_back.state == "rolled_back" and rolled_back.active_snapshot_revision == 1
    assert authority.active_snapshot().servers[0].host_connection.endpoint_identity == "calendar-v1"


def test_confirm_requires_exact_cas_and_command_cannot_change_semantics(tmp_path: Path) -> None:
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    preview = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview-1")
    with pytest.raises(MCPApprovedServerMigrationConflict, match="revision or state changed"):
        authority.confirm(migration_id=preview.migration_id, expected_revision=2, command_id="confirm-1", confirmed=True)
    confirmed = authority.confirm(migration_id=preview.migration_id, expected_revision=1, command_id="confirm-1", confirmed=True)
    assert authority.confirm(migration_id=preview.migration_id, expected_revision=1, command_id="confirm-1", confirmed=True) == confirmed
    with pytest.raises(MCPApprovedServerMigrationConflict, match="already used"):
        authority.begin_cutover(migration_id=preview.migration_id, expected_revision=confirmed.revision, command_id="confirm-1")


def test_legacy_and_unified_bootstrap_conflict_fails_closed(tmp_path: Path) -> None:
    _write_legacy(tmp_path, _candidate("calendar-v1", 1))
    unified = tmp_path / ".rebuild-data" / "security" / "mcp-approved-servers.json"
    unified.write_text(json.dumps(_candidate("calendar-v2", 2)), encoding="utf-8")
    with pytest.raises(Exception) as error:
        MCPApprovedServerMigrationAuthority(tmp_path).active_snapshot()
    assert "calendar-v" not in str(error.value)


@pytest.mark.parametrize("corruption", ["invalid-pointer", "invalid-snapshot", "database-bytes"])
def test_json_facade_normalizes_corrupt_durable_authority_to_store_error(tmp_path: Path, corruption: str) -> None:
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    authority.active_snapshot()
    path = tmp_path / ".rebuild-data" / "security" / "mcp-approved-server-authority.sqlite3"
    if corruption == "database-bytes":
        for sidecar in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if sidecar.exists():
                sidecar.unlink()
        path.write_bytes(b"not-a-sqlite-database")
    else:
        with sqlite3.connect(path) as conn:
            if corruption == "invalid-pointer":
                conn.execute("UPDATE mcp_approved_meta SET value = '999' WHERE key = 'active_snapshot_revision'")
            else:
                conn.execute("UPDATE mcp_approved_snapshots SET payload = ? WHERE snapshot_revision = 1", (b"{}",))
    with pytest.raises(MCPApprovedServerStoreError) as error:
        JsonMCPApprovedServerStore(tmp_path).snapshot()
    assert "sqlite" not in str(error.value).lower()
    assert "calendar" not in str(error.value).lower()


def test_noop_preview_is_rejected_and_affected_ids_are_internal_only(tmp_path: Path) -> None:
    _write_legacy(tmp_path, _candidate("calendar-v1", 1))
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    authority.active_snapshot()
    with pytest.raises(MCPApprovedServerMigrationConflict, match="no changes"):
        authority.preview(candidate=_candidate("calendar-v1", 1), command_id="noop-1")
    preview = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview-1")
    assert preview.affected_server_ids == ("calendar-server",)
    assert "affected_server_ids" not in preview.public()


def test_switching_is_global_pointer_bound_and_command_replay_is_historical(tmp_path: Path) -> None:
    _write_legacy(tmp_path, _candidate("calendar-v1", 1))
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    first = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview-first")
    second = authority.preview(candidate=_candidate("calendar-v3", 3), command_id="preview-second")
    first_confirmed = authority.confirm(migration_id=first.migration_id, expected_revision=1, command_id="confirm-first", confirmed=True)
    second_confirmed = authority.confirm(migration_id=second.migration_id, expected_revision=1, command_id="confirm-second", confirmed=True)
    first_fence = authority.begin_cutover(migration_id=first.migration_id, expected_revision=first_confirmed.revision, command_id="begin-first")
    assert authority.blocking_migration() == first_fence
    with pytest.raises(MCPApprovedServerMigrationConflict, match="switching"):
        authority.begin_cutover(migration_id=second.migration_id, expected_revision=second_confirmed.revision, command_id="begin-second")
    # Replaying a durable receipt must preserve the historical result rather
    # than project the migration's later state.
    assert authority.confirm(migration_id=first.migration_id, expected_revision=1, command_id="confirm-first", confirmed=True) == first_confirmed
    old_revoked = authority.mark_old_revoked(migration_id=first.migration_id, expected_revision=first_fence.revision, command_id="revoke-first")
    authority.commit_cutover(migration_id=first.migration_id, expected_revision=old_revoked.revision, command_id="commit-first")
    with pytest.raises(MCPApprovedServerMigrationConflict, match="pointer changed"):
        authority.begin_cutover(migration_id=second.migration_id, expected_revision=second_confirmed.revision, command_id="begin-second-after")


def test_each_switching_stage_rejects_pointer_drift(tmp_path: Path) -> None:
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    preview = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview-1")
    confirmed = authority.confirm(migration_id=preview.migration_id, expected_revision=1, command_id="confirm-1", confirmed=True)
    fence = authority.begin_cutover(migration_id=preview.migration_id, expected_revision=confirmed.revision, command_id="begin-1")
    _drift_pointer(tmp_path, 2)
    with pytest.raises(MCPApprovedServerMigrationConflict, match="pointer changed"):
        authority.mark_old_revoked(migration_id=preview.migration_id, expected_revision=fence.revision, command_id="revoke-1")


def test_cross_process_same_preview_command_commits_one_historical_result(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    candidate = _candidate("calendar-v2", 2)
    results = _run_process_race(ctx, [
        (_process_preview, (str(tmp_path), candidate, "preview-shared")),
        (_process_preview, (str(tmp_path), candidate, "preview-shared")),
    ])
    assert all(result[0] == "ok" for result in results)
    assert results[0][1:] == results[1][1:]
    database = tmp_path / ".rebuild-data" / "security" / "mcp-approved-server-authority.sqlite3"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mcp_approved_migrations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM mcp_approved_migration_commands WHERE command_id = 'preview-shared'").fetchone()[0] == 1


def test_cross_process_competing_cutovers_leave_one_switching_fence_and_pointer(tmp_path: Path) -> None:
    _write_legacy(tmp_path, _candidate("calendar-v1", 1))
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    first = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview-first")
    second = authority.preview(candidate=_candidate("calendar-v3", 3), command_id="preview-second")
    first = authority.confirm(migration_id=first.migration_id, expected_revision=1, command_id="confirm-first", confirmed=True)
    second = authority.confirm(migration_id=second.migration_id, expected_revision=1, command_id="confirm-second", confirmed=True)
    ctx = multiprocessing.get_context("spawn")
    results = _run_process_race(ctx, [
        (_process_begin, (str(tmp_path), first.migration_id, first.revision, "begin-first")),
        (_process_begin, (str(tmp_path), second.migration_id, second.revision, "begin-second")),
    ])
    assert sorted(result[0] for result in results) == ["error", "ok"]
    assert next(result for result in results if result[0] == "ok")[3] == "cutover_started"
    assert "switching" in next(result for result in results if result[0] == "error")[2]
    blocking = authority.blocking_migration()
    assert blocking is not None and blocking.state == "cutover_started"
    assert authority.active_snapshot().servers[0].host_connection.endpoint_identity == "calendar-v1"
    database = tmp_path / ".rebuild-data" / "security" / "mcp-approved-server-authority.sqlite3"
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM mcp_approved_migrations WHERE state IN "
            "('cutover_started', 'old_revoked', 'rollback_started', 'new_revoked')"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT value FROM mcp_approved_meta WHERE key = 'active_snapshot_revision'"
        ).fetchone()[0] == "1"


def test_cross_process_same_cutover_command_replays_one_transition_receipt(tmp_path: Path) -> None:
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    preview = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview-1")
    confirmed = authority.confirm(
        migration_id=preview.migration_id, expected_revision=1,
        command_id="confirm-1", confirmed=True,
    )
    ctx = multiprocessing.get_context("spawn")
    results = _run_process_race(ctx, [
        (_process_begin, (str(tmp_path), confirmed.migration_id, confirmed.revision, "begin-shared")),
        (_process_begin, (str(tmp_path), confirmed.migration_id, confirmed.revision, "begin-shared")),
    ])
    assert all(result[0] == "ok" for result in results)
    assert results[0][1:] == results[1][1:]
    assert authority.status(confirmed.migration_id).revision == confirmed.revision + 1
    database = tmp_path / ".rebuild-data" / "security" / "mcp-approved-server-authority.sqlite3"
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM mcp_approved_migration_commands WHERE command_id = 'begin-shared'"
        ).fetchone()[0] == 1


def test_cross_process_same_preview_command_with_different_candidate_keeps_first_receipt(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    results = _run_process_race(ctx, [
        (_process_preview, (str(tmp_path), _candidate("calendar-v2", 2), "preview-conflict")),
        (_process_preview, (str(tmp_path), _candidate("calendar-v3", 3), "preview-conflict")),
    ])
    assert sorted(result[0] for result in results) == ["error", "ok"]
    assert "already used" in next(result for result in results if result[0] == "error")[2]
    database = tmp_path / ".rebuild-data" / "security" / "mcp-approved-server-authority.sqlite3"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mcp_approved_migrations").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM mcp_approved_migration_commands WHERE command_id = 'preview-conflict'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("stage", ["begin", "commit", "rollback-begin", "rollback-commit"])
def test_every_remaining_switching_stage_rejects_pointer_drift(tmp_path: Path, stage: str) -> None:
    root = tmp_path / stage
    authority = MCPApprovedServerMigrationAuthority(root)
    preview = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview-1")
    confirmed = authority.confirm(migration_id=preview.migration_id, expected_revision=1, command_id="confirm-1", confirmed=True)
    if stage == "begin":
        _drift_pointer(root, 2)
        with pytest.raises(MCPApprovedServerMigrationConflict, match="pointer changed"):
            authority.begin_cutover(migration_id=preview.migration_id, expected_revision=confirmed.revision, command_id="begin-1")
        return
    fence = authority.begin_cutover(migration_id=preview.migration_id, expected_revision=confirmed.revision, command_id="begin-1")
    old_revoked = authority.mark_old_revoked(migration_id=preview.migration_id, expected_revision=fence.revision, command_id="revoke-1")
    if stage == "commit":
        _drift_pointer(root, 2)
        with pytest.raises(MCPApprovedServerMigrationConflict, match="pointer changed"):
            authority.commit_cutover(migration_id=preview.migration_id, expected_revision=old_revoked.revision, command_id="commit-1")
        return
    committed = authority.commit_cutover(migration_id=preview.migration_id, expected_revision=old_revoked.revision, command_id="commit-1")
    if stage == "rollback-begin":
        _drift_pointer(root, 1)
        with pytest.raises(MCPApprovedServerMigrationConflict, match="pointer changed"):
            authority.begin_rollback(migration_id=preview.migration_id, expected_revision=committed.revision, command_id="rollback-1")
        return
    rolling = authority.begin_rollback(migration_id=preview.migration_id, expected_revision=committed.revision, command_id="rollback-1")
    new_revoked = authority.mark_new_revoked(migration_id=preview.migration_id, expected_revision=rolling.revision, command_id="revoke-new-1")
    _drift_pointer(root, 1)
    with pytest.raises(MCPApprovedServerMigrationConflict, match="pointer changed"):
        authority.commit_rollback(migration_id=preview.migration_id, expected_revision=new_revoked.revision, command_id="rollback-commit-1")


def _drift_pointer(root: Path, revision: int) -> None:
    path = root / ".rebuild-data" / "security" / "mcp-approved-server-authority.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE mcp_approved_meta SET value = ? WHERE key = 'active_snapshot_revision'", (str(revision),))


def _write_legacy(root: Path, value: object) -> None:
    path = root / ".rebuild-data" / "security" / "mcp-approved-stdio.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _candidate(endpoint_identity: str, revision: int) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "servers": [{
            "server_id": "calendar-server", "enabled": True, "approval_status": "approved", "approval_revision": revision,
            "host_connection": {"server_id": "calendar-server", "manifest_revision": revision, "endpoint_identity": endpoint_identity, "credential_subject_id": "calendar-user", "transport_generation": revision, "catalog_revision": revision},
            "launch_manifest": {"server_id": "calendar-server", "manifest_revision": revision, "endpoint_identity": endpoint_identity, "credential_subject_id": "calendar-user", "transport_generation": revision, "approval_revision": revision, "approval_status": "approved", "executable": str(Path(sys.executable).resolve()), "argv": [], "cwd": None, "environment": None, "secret_env_refs": None},
            "tool_policies": [{"tool_name": "calendar.search", "tool_id": "calendar.search", "version": 1, "display_name": "Search", "description": "Reviewed", "effect": "read", "data_classes": ["calendar_event"], "input_schema_uri": "crp://input", "output_schema_uri": "crp://output", "receipt_schema_uri": None, "operation_semantics": "read_only", "execution_mode": "parallel", "resource_locks": ["mcp:calendar"], "idempotency": "never_retry", "retry_policy": {"max_attempts": 1, "backoff_ms": 0, "retryable_error_codes": []}, "verification_tool_id": None, "compensation_tool_id": None, "mutability": "read_only", "egress_class": "remote", "network_scope": ["mcp:calendar"], "data_egress_scope": ["calendar_event"], "timeout_ms": 1000, "required_scopes": ["calendar.read"], "boundary_requirements": ["mcp_enabled"], "requires_approval": False, "tool_schema_revision": 1, "reviewed_input_schema": {"type": "object"}, "reviewed_output_schema": None, "available": True, "remote_receipt_field": None, "reviewed_receipt_schema": None}],
        }],
    }


def test_preview_expected_active_revision_fails_before_persisting_candidate(tmp_path: Path) -> None:
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    active_revision, _snapshot = authority.active_snapshot_state()
    first = authority.preview(
        candidate=_candidate("calendar-v1", 1), command_id="first-preview",
        expected_active_snapshot_revision=active_revision,
    )
    confirmed = authority.confirm(
        migration_id=first.migration_id, expected_revision=first.revision,
        command_id="first-confirm", confirmed=True,
    )
    started = authority.begin_cutover(
        migration_id=confirmed.migration_id, expected_revision=confirmed.revision,
        command_id="first-begin",
    )
    revoked = authority.mark_old_revoked(
        migration_id=started.migration_id, expected_revision=started.revision,
        command_id="first-revoked",
    )
    authority.commit_cutover(
        migration_id=revoked.migration_id, expected_revision=revoked.revision,
        command_id="first-commit",
    )
    database = tmp_path / ".rebuild-data" / "security" / "mcp-approved-server-authority.sqlite3"
    with sqlite3.connect(database) as conn:
        before = (
            conn.execute("SELECT COUNT(*) FROM mcp_approved_snapshots").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM mcp_approved_migrations").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM mcp_approved_migration_commands").fetchone()[0],
        )

    with pytest.raises(MCPApprovedServerMigrationConflict, match="pointer changed"):
        authority.preview(
            candidate=_candidate("calendar-v2", 2), command_id="stale-preview",
            expected_active_snapshot_revision=active_revision,
        )

    with sqlite3.connect(database) as conn:
        after = (
            conn.execute("SELECT COUNT(*) FROM mcp_approved_snapshots").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM mcp_approved_migrations").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM mcp_approved_migration_commands").fetchone()[0],
        )
    assert after == before


def test_provenance_is_atomic_and_required_for_every_later_transition(
    tmp_path: Path,
) -> None:
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    active_revision, _snapshot = authority.active_snapshot_state()
    candidate = _candidate("calendar-v1", 1)
    candidate_payload, _parsed = canonical_mcp_approved_server_payload(candidate)
    provenance_ref = (
        "crp://external-extension-mcp-import-receipts/" + "a" * 40
    )
    provenance = {
        "schema_version": "1.0.0",
        "provenance_ref": provenance_ref,
        "kind": "external_extension_mcp_import_review",
        "project_id": "project-001",
        "extension_id": "mcp-calendar",
        "intake_ref": "crp://external-extension-intakes/calendar",
        "artifact_ref": "crp://external-extension-artifacts/calendar",
        "artifact_receipt_ref": "crp://external-extension-artifact-receipts/calendar",
        "artifact_content_sha256": "b" * 64,
        "manifest_identity": "c" * 64,
        "review_plan_identity": "d" * 64,
        "candidate_identity": f"blake3:{blake3(candidate_payload).hexdigest()}",
        "affected_server_ids": ["wrong-server"],
        "confirmation_ids": ["activate_external_extension"],
        "actor": "local-user",
        "reason": "Reviewed the exact disabled candidate.",
        "expected_active_snapshot_revision": active_revision,
    }
    with pytest.raises(
        MCPApprovedServerMigrationConflict,
        match="reviewed change set",
    ):
        authority.preview(
            candidate=candidate,
            command_id="provenance-mismatch",
            expected_active_snapshot_revision=active_revision,
            provenance_ref=provenance_ref,
            provenance=provenance,
        )

    database = (
        tmp_path / ".rebuild-data" / "security"
        / "mcp-approved-server-authority.sqlite3"
    )
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM mcp_approved_migration_provenance"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM mcp_approved_migrations"
        ).fetchone()[0] == 0

    provenance["affected_server_ids"] = ["calendar-server"]
    preview = authority.preview(
        candidate=candidate,
        command_id="provenance-preview",
        expected_active_snapshot_revision=active_revision,
        provenance_ref=provenance_ref,
        provenance=provenance,
    )
    assert preview.provenance_ref == provenance_ref
    with pytest.raises(
        MCPApprovedServerMigrationConflict,
        match="provenance confirmation is required",
    ):
        authority.confirm(
            migration_id=preview.migration_id,
            expected_revision=preview.revision,
            command_id="provenance-confirm-missing",
            confirmed=True,
        )
    confirmed = authority.confirm(
        migration_id=preview.migration_id,
        expected_revision=preview.revision,
        command_id="provenance-confirm",
        confirmed=True,
        provenance_ref=provenance_ref,
    )
    with pytest.raises(
        MCPApprovedServerMigrationConflict,
        match="provenance confirmation is required",
    ):
        authority.begin_cutover(
            migration_id=confirmed.migration_id,
            expected_revision=confirmed.revision,
            command_id="provenance-cutover-missing",
        )
