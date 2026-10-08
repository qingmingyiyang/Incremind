from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from core.storage_provider import (
    VaultOperationalRecoveryError,
    adopt_prepared_vault,
    load_vault_recovery_operation,
    recover_vault_operation,
)


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
        if args.recover_pending:
            if args.operation_id:
                raise VaultOperationalRecoveryError(
                    "operation id is not accepted with recover-pending"
                )
            results = _recover_pending(operations)
            _emit({"status": "reconciled", "operations": results})
            return 0
        operation_id = str(args.operation_id or "")
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise VaultOperationalRecoveryError("Vault recovery operation id is invalid")
        operation = load_vault_recovery_operation(
            operations_root=operations, operation_id=operation_id
        )
        if Path(operation.active_root).resolve(strict=False) != root:
            raise VaultOperationalRecoveryError(
                "Vault recovery operation does not belong to working root"
            )
        result = adopt_prepared_vault(
            operations_root=operations,
            operation_id=operation_id,
            application_offline=True,
        )
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


def _recover_pending(operations: Path) -> list[dict[str, object]]:
    if not operations.exists():
        return []
    if not operations.is_dir() or operations.is_symlink():
        raise VaultOperationalRecoveryError("Vault recovery operations root is unsafe")
    results: list[dict[str, object]] = []
    for path in sorted(operations.glob("*.json"), key=lambda item: item.name):
        operation_id = path.stem
        if path.is_symlink() or _OPERATION_ID.fullmatch(operation_id) is None:
            raise VaultOperationalRecoveryError("Vault recovery operation entry is unsafe")
        operation = load_vault_recovery_operation(
            operations_root=operations, operation_id=operation_id
        )
        if operation.state == "failed":
            results.append({"operation_id": operation_id, "state": "failed"})
            continue
        recovered = recover_vault_operation(
            operations_root=operations,
            operation_id=operation_id,
            application_offline=True,
        )
        results.append({"operation_id": operation_id, "state": recovered.state})
    return results


def _emit(payload: dict[str, object], *, stream=None) -> None:
    stream = sys.stdout if stream is None else stream
    stream.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    stream.flush()


if __name__ == "__main__":
    raise SystemExit(main())
