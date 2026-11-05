from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from backend.shared.deployment import runtime_backup_roots
from core.storage_provider import (
    VaultOperationalRecoveryError,
    adopt_prepared_vault,
    load_vault_recovery_operation,
    recover_vault_operation,
)
from backend.memory_app.runtime_recovery import (
    adopt_prepared_runtime,
    load_runtime_recovery_operation,
    recovery_operation_kind,
)
from core.storage_provider.vault_operational_recovery import _remove_stale_lease


_OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chriptmas-vault-recovery")
    parser.add_argument("--working-root", required=True)
    parser.add_argument("--operation-id")
    parser.add_argument("--recover-pending", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.working_root).expanduser().absolute().resolve(strict=False)
    operations = root.parent / f".{root.name}-recovery" / "operations"
    try:
        _reconcile_dead_lease(operations, root)
        if args.recover_pending:
            if args.operation_id:
                raise VaultOperationalRecoveryError(
                    "operation id is not accepted with recover-pending"
                )
            results = _recover_pending(operations, root)
            _emit({"status": "reconciled", "operations": results})
            return 0
        operation_id = str(args.operation_id or "")
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise VaultOperationalRecoveryError("Vault recovery operation id is invalid")
        result = _adopt_operation(operations, operation_id, root)
        _emit(
            {
                "status": result.state,
                "operation_id": result.operation_id,
                "file_count": result.file_count,
            }
        )
        return 0
    except (OSError, VaultOperationalRecoveryError) as error:
        _emit({"status": "failed", "error": type(error).__name__}, stream=sys.stderr)
        return 2


def _recover_pending(operations: Path, root: Path) -> list[dict[str, object]]:
    if not operations.exists():
        return []
    if not operations.is_dir() or operations.is_symlink():
        raise VaultOperationalRecoveryError("Vault recovery operations root is unsafe")
    results: list[dict[str, object]] = []
    for path in sorted(operations.glob("*.json"), key=lambda item: item.name):
        operation_id = path.stem
        if path.is_symlink() or _OPERATION_ID.fullmatch(operation_id) is None:
            raise VaultOperationalRecoveryError("Vault recovery operation entry is unsafe")
        runtime = recovery_operation_kind(operations_root=operations, operation_id=operation_id) == "runtime"
        operation = (load_runtime_recovery_operation if runtime else load_vault_recovery_operation)(
            operations_root=operations, operation_id=operation_id)
        _require_working_root(operation, root, runtime=runtime)
        if operation.state == "failed":
            results.append({"operation_id": operation_id, "state": "failed"})
            continue
        recovered = (_adopt_operation(operations, operation_id, root) if runtime else recover_vault_operation(
            operations_root=operations, operation_id=operation_id, application_offline=True))
        results.append({"operation_id": operation_id, "state": recovered.state})
    return results


def _adopt_operation(operations: Path, operation_id: str, root: Path):
    runtime = recovery_operation_kind(operations_root=operations, operation_id=operation_id) == "runtime"
    if runtime:
        return adopt_prepared_runtime(
            operations_root=operations, operation_id=operation_id,
            working_root=root, application_offline=True,
        )
    operation = load_vault_recovery_operation(operations_root=operations, operation_id=operation_id)
    _require_working_root(operation, root, runtime=False)
    return adopt_prepared_vault(operations_root=operations, operation_id=operation_id, application_offline=True)


def _require_working_root(operation, root: Path, *, runtime: bool) -> None:
    expected = root.parent if runtime and root.name == ".rebuild-data" else root
    if runtime and root.name != ".rebuild-data" or Path(operation.active_root).resolve(strict=False) != expected:
        raise VaultOperationalRecoveryError("Vault recovery operation does not belong to working root")


def _reconcile_dead_lease(operations: Path, root: Path) -> None:
    lease = root.parent / ".chriptmas-vault-recovery.lock"
    if lease.is_symlink():
        raise VaultOperationalRecoveryError("recovery lease is unsafe")
    if not lease.exists():
        return
    try:
        payload = json.loads(lease.read_text(encoding="utf8"))
        operation_id = payload["operation_id"]
        owner = payload["pid"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise VaultOperationalRecoveryError("recovery lease is unreadable") from error
    if (not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None
            or type(owner) is not int or owner <= 0):
        raise VaultOperationalRecoveryError("recovery lease operation is invalid")
    journal = operations / f"{operation_id}.json"
    if not journal.exists() and not journal.is_symlink():
        _require_unrecorded_runtime_lease(operations, root, operation_id)
    else:
        runtime = recovery_operation_kind(operations_root=operations, operation_id=operation_id) == "runtime"
        operation = (load_runtime_recovery_operation if runtime else load_vault_recovery_operation)(
            operations_root=operations, operation_id=operation_id)
        _require_working_root(operation, root, runtime=runtime)
    if not _remove_stale_lease(lease, operation_id):
        raise VaultOperationalRecoveryError("recovery lease still has a live or invalid owner")


def _require_unrecorded_runtime_lease(operations: Path, root: Path, operation_id: str) -> None:
    working, _snapshots, expected = runtime_backup_roots(root.parent)
    if root.name != ".rebuild-data" or root != working or not root.is_dir() or operations != expected:
        raise VaultOperationalRecoveryError("unrecorded recovery lease does not belong to the fixed runtime root")
    traces = [expected.parent / name / operation_id for name in ("restored", "rollback")]
    traces.extend(root.parent / f".{root.name}.{name}-{operation_id}" for name in ("restore", "rollback", "displaced"))
    directories = (operations, expected.parent, expected.parent / "restored", expected.parent / "rollback")
    if any(path.exists() and not path.is_dir() for path in directories):
        raise VaultOperationalRecoveryError("unrecorded recovery lease path is unsafe")
    if any(candidate.is_symlink() for path in (root, operations, *traces) for candidate in (path, *path.parents)):
        raise VaultOperationalRecoveryError("unrecorded recovery lease path is unsafe")
    if any(path.exists() for path in traces):
        raise VaultOperationalRecoveryError("unrecorded recovery lease has restore artifacts")


def _emit(payload: dict[str, object], *, stream=None) -> None:
    stream = sys.stdout if stream is None else stream
    stream.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    stream.flush()


if __name__ == "__main__":
    raise SystemExit(main())
