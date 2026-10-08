from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Vault recovery from a packaged sidecar")
    parser.add_argument("--candidate-sidecar", required=True, type=Path)
    parser.add_argument("--expected-content-set", required=True)
    return parser.parse_args()


def _vault(root: Path, name: str, value: str) -> Path:
    vault = root / name
    (vault / "library").mkdir(parents=True)
    (vault / "library" / "note.md").write_text(value, encoding="utf-8")
    (vault / ".rebuild-data").mkdir()
    (vault / ".rebuild-data" / "state.json").write_text(
        json.dumps({"value": value}), encoding="utf-8"
    )
    return vault


def _read(vault: Path) -> str:
    return (vault / "library" / "note.md").read_text(encoding="utf-8")


def _prepared(root: Path, operation_id: str):
    from rebuild.storage_provider import create_vault_backup, prepare_vault_recovery

    desired = _vault(root, "desired", "verified backup")
    active = _vault(root, "active", "current authority")
    snapshot = create_vault_backup(
        source_root=desired,
        backups_root=root / "backups",
        snapshot_id=f"snapshot-{operation_id}",
    )
    operations = root / "operations"
    operation = prepare_vault_recovery(
        snapshot_root=snapshot.snapshot_root,
        active_root=active,
        operations_root=operations,
        operation_id=operation_id,
    )
    return operation, operations, active, snapshot.snapshot_root


def _expect(error_type, message: str, action) -> None:
    try:
        action()
    except error_type as error:
        if message not in str(error):
            raise AssertionError(f"unexpected error: {error}") from error
    else:
        raise AssertionError(f"expected {error_type.__name__}")


def _child_env(sidecar: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(sidecar)
    return environment


def _kill_and_recover(root: Path, sidecar: Path, stage: str, rollback: bool) -> None:
    from rebuild.storage_provider import (
        adopt_prepared_vault,
        recover_vault_operation,
    )

    operation, operations, active, _snapshot = _prepared(root, f"kill-{stage}")
    if rollback:
        adopt_prepared_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=True,
        )
    function_name = "rollback_adopted_vault" if rollback else "adopt_prepared_vault"
    exit_code = 78 if rollback else 77
    script = f"""
import os, sys
from pathlib import Path
from rebuild.storage_provider import {function_name}
def kill(current):
    if current == sys.argv[3]:
        os._exit({exit_code})
{function_name}(
    operations_root=Path(sys.argv[1]),
    operation_id=sys.argv[2],
    application_offline=True,
    fault_hook=kill,
)
"""
    interrupted = subprocess.run(
        [sys.executable, "-c", script, str(operations), operation.operation_id, stage],
        check=False,
        capture_output=True,
        timeout=30,
        env=_child_env(sidecar),
    )
    if interrupted.returncode != exit_code:
        raise AssertionError(
            f"kill stage {stage} returned {interrupted.returncode}: "
            + interrupted.stderr.decode("utf-8", errors="replace")
        )
    recovered = recover_vault_operation(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )
    expected_state = "rolled_back" if rollback else "adopted"
    if recovered.state != expected_state:
        raise AssertionError(f"{stage} recovered to {recovered.state}")
    expected_value = "current authority" if rollback else "verified backup"
    if _read(active) != expected_value:
        raise AssertionError(f"{stage} active authority mismatch")


def main() -> int:
    args = _args()
    sidecar = args.candidate_sidecar.resolve(strict=True)
    manifest = json.loads((sidecar / "sidecar-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("content_set_sha256") != args.expected_content_set:
        raise AssertionError("candidate content-set mismatch")
    sys.path.insert(0, str(sidecar))

    from rebuild.storage_provider import (
        VaultBackupRestoreError,
        VaultOperationalRecoveryConflict,
        VaultOperationalRecoveryError,
        adopt_prepared_vault,
        create_vault_backup,
        load_vault_recovery_operation,
        prepare_vault_recovery,
        rollback_adopted_vault,
    )

    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="chriptmas-release-vault-") as temporary:
        root = Path(temporary)

        operation, operations, active, snapshot = _prepared(root / "roundtrip", "roundtrip")
        _expect(
            VaultOperationalRecoveryConflict,
            "offline",
            lambda: adopt_prepared_vault(
                operations_root=operations,
                operation_id=operation.operation_id,
                application_offline=False,
            ),
        )
        adopted = adopt_prepared_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=True,
        )
        replayed = adopt_prepared_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=True,
        )
        if replayed != adopted or _read(active) != "verified backup":
            raise AssertionError("adoption replay mismatch")
        rolled_back = rollback_adopted_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=True,
        )
        if rollback_adopted_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=True,
        ) != rolled_back:
            raise AssertionError("rollback replay mismatch")
        if _read(active) != "current authority" or _read(Path(rolled_back.displaced_root)) != "verified backup":
            raise AssertionError("rollback did not preserve both authorities")
        checks.extend(["offline_confirmation", "adopt_rollback", "idempotent_replay"])

        drift_operation, drift_operations, drift_active, _ = _prepared(root / "drift", "drift")
        (drift_active / "library" / "note.md").write_text("drifted", encoding="utf-8")
        _expect(
            VaultOperationalRecoveryConflict,
            "fingerprint drifted",
            lambda: adopt_prepared_vault(
                operations_root=drift_operations,
                operation_id=drift_operation.operation_id,
                application_offline=True,
            ),
        )
        checks.append("authority_drift_rejected")

        corrupt_root = root / "corrupt"
        desired = _vault(corrupt_root, "desired", "verified backup")
        corrupt_active = _vault(corrupt_root, "active", "current authority")
        corrupt_snapshot = create_vault_backup(
            source_root=desired,
            backups_root=corrupt_root / "backups",
            snapshot_id="snapshot-corrupt",
        )
        (corrupt_snapshot.snapshot_root / "payload" / "library" / "note.md").write_text(
            "tampered", encoding="utf-8"
        )
        _expect(
            VaultBackupRestoreError,
            "hash",
            lambda: prepare_vault_recovery(
                snapshot_root=corrupt_snapshot.snapshot_root,
                active_root=corrupt_active,
                operations_root=corrupt_root / "operations",
                operation_id="corrupt",
            ),
        )
        checks.append("snapshot_drift_rejected")

        disk_root = root / "disk-full"
        desired = _vault(disk_root, "desired", "verified backup")
        disk_active = _vault(disk_root, "active", "current authority")
        disk_snapshot = create_vault_backup(
            source_root=desired,
            backups_root=disk_root / "backups",
            snapshot_id="snapshot-disk-full",
        )
        disk_operations = disk_root / "operations"

        def disk_full(_source: Path, _target: Path) -> None:
            raise OSError(28, "No space left on device")

        _expect(
            VaultOperationalRecoveryError,
            "did not complete",
            lambda: prepare_vault_recovery(
                snapshot_root=disk_snapshot.snapshot_root,
                active_root=disk_active,
                operations_root=disk_operations,
                operation_id="disk-full",
                copy_file=disk_full,
            ),
        )
        failed = load_vault_recovery_operation(
            operations_root=disk_operations, operation_id="disk-full"
        )
        if failed.state != "failed" or _read(disk_active) != "current authority":
            raise AssertionError("disk-full failure mutated active authority")
        checks.append("disk_full_fail_closed")

        for stage in ("old_detached", "new_adopted"):
            _kill_and_recover(root / stage, sidecar, stage, rollback=False)
            checks.append(f"kill_recovery_{stage}")
        for stage in ("adopted_displaced", "old_restored"):
            _kill_and_recover(root / stage, sidecar, stage, rollback=True)
            checks.append(f"kill_recovery_{stage}")

        if not snapshot.is_dir():
            raise AssertionError("snapshot unexpectedly missing")

    print(
        json.dumps(
            {
                "status": "passed",
                "candidate_content_set": args.expected_content_set,
                "packaged_python": sys.version.split()[0],
                "temporary_vault_only": True,
                "checks": checks,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
