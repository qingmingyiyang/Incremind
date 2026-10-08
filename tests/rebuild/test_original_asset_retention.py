from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import os

import pytest

from core.product_core import (
    BuildOriginalAssetRetentionPlan,
    ExecuteOriginalAssetRetentionPurge,
    OriginalAssetBackupEvidence,
    OriginalAssetRetentionError,
    ReconcileOriginalAssetOrphans,
    RetentionBackupEvidence,
)
from core.storage_provider import JsonObjectStore
from core.effect_log import EffectReaper, EffectState, shared_effect_runner


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64
BODY = b"original-asset-retention-canary"
SHA256 = hashlib.sha256(BODY).hexdigest()
ASSET_ID = f"original-file-{SHA256[:16]}"
VAULT_REF = f"assets/originals/{SHA256[:2]}/{ASSET_ID}.txt"


def _runner(tmp_path):
    return shared_effect_runner(
        tmp_path / "original-asset-retention-effects.sqlite3",
        owner_role="test-original-asset-retention",
        lease_seconds=300,
    )


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "legacy",
    )


def _backup() -> RetentionBackupEvidence:
    return RetentionBackupEvidence(
        status="verified",
        snapshot_id="snapshot-original-asset-retention",
        snapshot_fingerprint=FINGERPRINT,
        active_fingerprint=FINGERPRINT,
        file_count=9,
    )


def _asset_backup() -> OriginalAssetBackupEvidence:
    return OriginalAssetBackupEvidence(
        status="verified",
        backup_id="asset-backup-original-retention",
        sha256=SHA256,
        byte_count=len(BODY),
    )


def _write_asset(
    store: JsonObjectStore,
    tmp_path,
    *,
    asset_id: str = ASSET_ID,
    vault_ref: str = VAULT_REF,
    sha256: str = SHA256,
    body: bytes = BODY,
    orphaned_at: str | None = "2026-07-01T00:00:00Z",
):
    path = tmp_path / "library" / vault_ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    record = {
        "schema_version": "1.0.0",
        "id": asset_id,
        "kind": "workbench_original_asset",
        "sha256": sha256,
        "vault_ref": vault_ref,
        "byte_count": len(body),
        "link_status": "orphaned" if orphaned_at else "pending",
        "orphaned_at": orphaned_at,
        "orphan_reason": (
            "no_active_source_asset_links" if orphaned_at else "awaiting_source_capture"
        ),
        "display_name": "private-canary.txt",
    }
    store.write("workbench_original_assets", asset_id, record, expected_revision=0)
    return path


def _plan(store, tmp_path, *, asset_id: str = ASSET_ID):
    return BuildOriginalAssetRetentionPlan(
        store,
        library_root=tmp_path / "library",
    ).execute(
        asset_id=asset_id,
        backup_evidence=_backup(),
        asset_backup_evidence=_asset_backup(),
        observed_vault_fingerprint=FINGERPRINT,
        evaluated_at=NOW,
    )


def test_reconcile_starts_orphan_clock_and_resets_it_when_link_returns(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    _write_asset(store, tmp_path, orphaned_at=None)
    service = ReconcileOriginalAssetOrphans(store, clock=lambda: NOW)

    orphaned = service.execute()[0]
    stable = service.execute()[0]
    store.write(
        "source_asset_links",
        "link-source-one",
        {"id": "link-source-one", "source_id": "source-one", "asset_id": ASSET_ID},
        expected_revision=0,
    )
    linked = service.execute()[0]

    assert orphaned.status == "orphaned"
    assert orphaned.orphaned_at == "2026-07-27T12:00:00Z"
    assert stable.revision == orphaned.revision
    assert linked.status == "linked"
    assert linked.orphaned_at is None
    assert linked.revision == orphaned.revision + 1
    current = store.read("workbench_original_assets", ASSET_ID)
    assert current is not None
    assert current["orphaned_at"] is None
    assert current["orphan_reason"] is None


def test_plan_blocks_linked_recent_unverified_and_drifted_assets(tmp_path) -> None:
    store = _store(tmp_path)
    path = _write_asset(store, tmp_path)
    store.write(
        "source_asset_links",
        "link-source-one",
        {"id": "link-source-one", "source_id": "source-one", "asset_id": ASSET_ID},
        expected_revision=0,
    )
    linked = _plan(store, tmp_path)
    assert linked.candidate.eligible is False
    assert linked.candidate.blockers == ("source_asset_links_present",)

    store.delete("source_asset_links", "link-source-one")
    current = dict(store.read("workbench_original_assets", ASSET_ID) or {})
    current["orphaned_at"] = "2026-07-25T00:00:00Z"
    store.write(
        "workbench_original_assets",
        ASSET_ID,
        current,
        expected_revision=1,
    )
    recent = _plan(store, tmp_path)
    assert "retention_not_elapsed" in recent.candidate.blockers

    current["orphaned_at"] = "2026-07-01T00:00:00Z"
    store.write(
        "workbench_original_assets",
        ASSET_ID,
        current,
        expected_revision=2,
    )
    path.write_bytes(b"drifted")
    drifted = _plan(store, tmp_path)
    assert "asset_file_drifted" in drifted.candidate.blockers

    unverified = BuildOriginalAssetRetentionPlan(
        store,
        library_root=tmp_path / "library",
    ).execute(
        asset_id=ASSET_ID,
        backup_evidence=replace(_backup(), status="missing"),
        asset_backup_evidence=_asset_backup(),
        observed_vault_fingerprint=FINGERPRINT,
        evaluated_at=NOW,
    )
    assert "backup_proof_invalid" in unverified.candidate.blockers

    invalid_asset_backup = BuildOriginalAssetRetentionPlan(
        store,
        library_root=tmp_path / "library",
    ).execute(
        asset_id=ASSET_ID,
        backup_evidence=_backup(),
        asset_backup_evidence=replace(_asset_backup(), sha256="0" * 64),
        observed_vault_fingerprint=FINGERPRINT,
        evaluated_at=NOW,
    )
    assert "asset_backup_proof_invalid" in invalid_asset_backup.candidate.blockers


def test_purge_deletes_verified_file_then_authority_and_replays(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    path = _write_asset(store, tmp_path)
    plan = _plan(store, tmp_path)
    assert plan.candidate.eligible is True
    steps: list[str] = []
    service = ExecuteOriginalAssetRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        library_root=tmp_path / "library",
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: NOW,
        after_step=steps.append,
    )

    first = service.execute(
        plan=plan,
        expected_asset_revision=1,
        confirm=True,
    )
    replay = service.execute(
        plan=plan,
        expected_asset_revision=1,
        confirm=True,
    )

    assert first.status == "completed"
    assert first.byte_action == "delete"
    assert first.idempotent is False
    assert replay.idempotent is True
    assert steps == ["bytes_staged", "authority", "bytes_deleted"]
    assert not path.exists()
    assert store.read("workbench_original_assets", ASSET_ID) is None
    receipt = store.read("original_asset_retention_receipts", first.operation_id)
    assert receipt is not None
    assert receipt["kind"] == "original_asset_retention_receipt"
    assert "display_name" not in receipt
    assert BODY.decode() not in str(receipt)


def test_purge_resumes_after_file_delete_crash(tmp_path) -> None:
    store = _store(tmp_path)
    path = _write_asset(store, tmp_path)
    plan = _plan(store, tmp_path)
    clock = [NOW]
    crashed = False

    def fail_once(step: str) -> None:
        nonlocal crashed
        if step == "bytes_staged" and not crashed:
            crashed = True
            raise RuntimeError("simulated crash")

    service = ExecuteOriginalAssetRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        library_root=tmp_path / "library",
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: clock[0],
        after_step=fail_once,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.execute(plan=plan, expected_asset_revision=1, confirm=True)

    assert not path.exists()
    assert store.read("workbench_original_assets", ASSET_ID) is not None
    clock[0] = datetime.fromtimestamp(NOW.timestamp() + 301, tz=UTC)
    recovered = EffectReaper(service._runner.log).recover_expired(
        now=int(clock[0].timestamp()),
        probes={("original_asset_retention_purge", "effect-v2"): lambda effect: service.verify_effect(effect.operation_id)},
        verifiers={("original_asset_retention_purge", "effect-v2"): lambda effect: service.verify_effect(effect.operation_id)},
    )
    assert recovered[0].state is EffectState.PLANNED
    completed = service.execute(
        plan=plan,
        expected_asset_revision=1,
        confirm=True,
    )
    assert completed.status == "completed"
    assert store.read("workbench_original_assets", ASSET_ID) is None


def test_purge_resumes_after_authority_delete_before_quarantine_cleanup(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    path = _write_asset(store, tmp_path)
    plan = _plan(store, tmp_path)
    clock = [NOW]
    crashed = False

    def fail_once(step: str) -> None:
        nonlocal crashed
        if step == "authority" and not crashed:
            crashed = True
            raise RuntimeError("simulated authority crash")

    service = ExecuteOriginalAssetRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        library_root=tmp_path / "library",
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: clock[0],
        after_step=fail_once,
    )
    with pytest.raises(RuntimeError, match="simulated authority crash"):
        service.execute(plan=plan, expected_asset_revision=1, confirm=True)

    assert not path.exists()
    assert store.read("workbench_original_assets", ASSET_ID) is None
    clock[0] = datetime.fromtimestamp(NOW.timestamp() + 301, tz=UTC)
    recovered = EffectReaper(service._runner.log).recover_expired(
        now=int(clock[0].timestamp()),
        probes={("original_asset_retention_purge", "effect-v2"): lambda effect: service.verify_effect(effect.operation_id)},
        verifiers={("original_asset_retention_purge", "effect-v2"): lambda effect: service.verify_effect(effect.operation_id)},
    )
    assert recovered[0].state is EffectState.PLANNED
    completed = service.execute(
        plan=plan,
        expected_asset_revision=1,
        confirm=True,
    )
    assert completed.status == "completed"


def test_original_asset_restarted_handler_resumes_from_immutable_intent(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    _write_asset(store, tmp_path)
    plan = _plan(store, tmp_path)
    clock = [NOW]
    crashed = False

    def fail_once(step: str) -> None:
        nonlocal crashed
        if step == "authority" and not crashed:
            crashed = True
            raise RuntimeError("simulated process crash")

    first = ExecuteOriginalAssetRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        library_root=tmp_path / "library",
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: clock[0],
        after_step=fail_once,
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.execute(plan=plan, expected_asset_revision=1, confirm=True)
    operation_id = store.list("original_asset_retention_intents")[0]["id"]
    clock[0] = datetime.fromtimestamp(NOW.timestamp() + 301, tz=UTC)
    EffectReaper(first._runner.log).recover_expired(
        now=int(clock[0].timestamp()),
        probes={("original_asset_retention_purge", "effect-v2"): lambda effect: first.verify_effect(effect.operation_id)},
        verifiers={("original_asset_retention_purge", "effect-v2"): lambda effect: first.verify_effect(effect.operation_id)},
    )
    restarted_runner = shared_effect_runner(
        tmp_path / "original-asset-retention-effects.sqlite3",
        owner_role="restarted-original-asset-retention",
        lease_seconds=300,
    )
    restarted = ExecuteOriginalAssetRetentionPurge(
        store,
        effect_runner=restarted_runner,
        library_root=tmp_path / "library",
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: clock[0],
    )
    outcome = restarted_runner.execute_planned(
        operation_id, restarted.handle_effect, now=int(clock[0].timestamp()),
    )
    assert outcome.state is EffectState.SETTLED_OK
    assert store.read("workbench_original_assets", ASSET_ID) is None


def test_original_asset_receipt_before_settle_waits_for_core_reaper(
    tmp_path, monkeypatch,
) -> None:
    store = _store(tmp_path)
    _write_asset(store, tmp_path)
    plan = _plan(store, tmp_path)
    clock = [NOW]
    service = ExecuteOriginalAssetRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        library_root=tmp_path / "library",
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: clock[0],
    )

    def crash_before_settle(*_args, **_kwargs):
        raise BaseException("simulated process exit before Effect settle")

    original_settle = service._runner.settle_ok
    monkeypatch.setattr(service._runner, "settle_ok", crash_before_settle)
    with pytest.raises(BaseException, match="before Effect settle"):
        service.execute(plan=plan, expected_asset_revision=1, confirm=True)
    operation_id = store.list("original_asset_retention_receipts")[0]["id"]
    assert store.read("original_asset_retention_receipts", operation_id) is not None
    assert service._runner.log.get(operation_id).state is EffectState.INFLIGHT

    monkeypatch.setattr(service._runner, "settle_ok", original_settle)
    with pytest.raises(OriginalAssetRetentionError, match="not settled"):
        service.execute(plan=plan, expected_asset_revision=1, confirm=True)
    clock[0] = datetime.fromtimestamp(NOW.timestamp() + 301, tz=UTC)
    recovered = EffectReaper(service._runner.log).recover_expired(
        now=int(clock[0].timestamp()),
        probes={("original_asset_retention_purge", "effect-v2"): lambda effect: service.verify_effect(effect.operation_id)},
        verifiers={("original_asset_retention_purge", "effect-v2"): lambda effect: service.verify_effect(effect.operation_id)},
    )
    assert recovered[0].state is EffectState.SETTLED_OK
    assert service.execute(
        plan=plan, expected_asset_revision=1, confirm=True,
    ).idempotent is True


def test_shared_bytes_keep_file_while_orphan_authority_is_removed(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    path = _write_asset(store, tmp_path)
    shared_id = "original-file-shared-copy"
    shared = dict(store.read("workbench_original_assets", ASSET_ID) or {})
    shared["id"] = shared_id
    shared["link_status"] = "linked"
    shared["orphaned_at"] = None
    store.write(
        "workbench_original_assets",
        shared_id,
        shared,
        expected_revision=0,
    )
    store.write(
        "source_asset_links",
        "link-shared",
        {"id": "link-shared", "source_id": "source-two", "asset_id": shared_id},
        expected_revision=0,
    )
    plan = _plan(store, tmp_path)
    assert plan.candidate.eligible is True
    assert plan.candidate.byte_action == "retain_shared"

    result = ExecuteOriginalAssetRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        library_root=tmp_path / "library",
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: NOW,
    ).execute(plan=plan, expected_asset_revision=1, confirm=True)

    assert result.byte_action == "retain_shared"
    assert path.read_bytes() == BODY
    assert store.read("workbench_original_assets", ASSET_ID) is None
    assert store.read("workbench_original_assets", shared_id) is not None


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("unconfirmed", "explicit confirmation"),
        ("stale_revision", "revision mismatch"),
        ("vault_drift", "active Vault drifted"),
        ("link_reappeared", "gained a Source link"),
        ("shared_state_drift", "shared-byte state drifted"),
    ],
)
def test_purge_fails_closed_before_receipt(
    tmp_path,
    mutation: str,
    error: str,
) -> None:
    store = _store(tmp_path)
    path = _write_asset(store, tmp_path)
    plan = _plan(store, tmp_path)
    confirm = mutation != "unconfirmed"
    revision = 2 if mutation == "stale_revision" else 1
    fingerprint = "b" * 64 if mutation == "vault_drift" else FINGERPRINT
    if mutation == "link_reappeared":
        store.write(
            "source_asset_links",
            "link-late",
            {"id": "link-late", "source_id": "source-late", "asset_id": ASSET_ID},
            expected_revision=0,
        )
    if mutation == "shared_state_drift":
        shared = dict(store.read("workbench_original_assets", ASSET_ID) or {})
        shared["id"] = "original-file-late-shared"
        store.write(
            "workbench_original_assets",
            shared["id"],
            shared,
            expected_revision=0,
        )

    with pytest.raises(OriginalAssetRetentionError, match=error):
        ExecuteOriginalAssetRetentionPurge(
            store,
            effect_runner=_runner(tmp_path),
            library_root=tmp_path / "library",
            active_fingerprint=lambda: fingerprint,
            clock=lambda: NOW,
        ).execute(
            plan=plan,
            expected_asset_revision=revision,
            confirm=confirm,
        )

    assert path.exists()
    assert store.read("workbench_original_assets", ASSET_ID) is not None
    assert store.list("original_asset_retention_intents") == ()
    assert store.list("original_asset_retention_receipts") == ()


def test_symlink_or_reparse_asset_is_never_eligible(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    path = _write_asset(store, tmp_path)
    target = tmp_path / "outside.txt"
    target.write_bytes(BODY)
    path.unlink()
    try:
        os.symlink(target, path)
    except OSError:
        path.write_bytes(BODY)
        path_type = type(path)
        original_is_symlink = path_type.is_symlink
        monkeypatch.setattr(
            path_type,
            "is_symlink",
            lambda self: self == path or original_is_symlink(self),
        )

    plan = _plan(store, tmp_path)

    assert plan.candidate.eligible is False
    assert "asset_file_boundary_invalid" in plan.candidate.blockers
    assert target.read_bytes() == BODY
