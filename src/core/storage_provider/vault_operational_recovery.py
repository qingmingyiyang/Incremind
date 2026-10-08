from __future__ import annotations

import ctypes
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .vault_backup_restore import (
    compare_vault_to_backup,
    fingerprint_vault_root,
    fingerprint_vault_restore_source,
    restore_vault_backup,
    verify_vault_backup,
)


_SAFE_OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_SCHEMA_VERSION = "1.0.0"


class VaultOperationalRecoveryError(ValueError):
    """Raised when offline Vault recovery cannot safely converge."""


class VaultOperationalRecoveryConflict(VaultOperationalRecoveryError):
    """Raised for concurrent or contradictory recovery state."""


@dataclass(frozen=True, slots=True)
class VaultRecoveryOperation:
    schema_version: str
    operation_id: str
    state: str
    intent: str
    snapshot_root: str
    active_root: str
    staging_root: str
    rollback_root: str
    displaced_root: str
    snapshot_fingerprint: str
    original_fingerprint: str
    file_count: int
    last_error: str | None = None
    original_fingerprint_kind: str = "physical-v1"


def prepare_vault_recovery(
    *,
    snapshot_root: Path,
    active_root: Path,
    operations_root: Path,
    operation_id: str,
    copy_file: Callable[[Path, Path], None] | None = None,
    expected_source_fingerprint: str | None = None,
) -> VaultRecoveryOperation:
    _require_operation_id(operation_id)
    active = _existing_directory(active_root, "active Vault")
    operations = _safe_absolute(operations_root)
    if _contains(active, operations) or _contains(operations, active):
        raise VaultOperationalRecoveryConflict("recovery operations root cannot overlap active Vault")
    snapshot = verify_vault_backup(snapshot_root)
    if _paths_overlap(snapshot.snapshot_root, active):
        raise VaultOperationalRecoveryConflict("recovery snapshot cannot overlap active Vault")
    staging = active.parent / f".{active.name}.restore-{operation_id}"
    rollback = active.parent / f".{active.name}.rollback-{operation_id}"
    displaced = active.parent / f".{active.name}.displaced-{operation_id}"
    logical = expected_source_fingerprint is not None
    original_fingerprint = expected_source_fingerprint if logical else fingerprint_vault_root(active)
    operation = VaultRecoveryOperation(
        schema_version=_SCHEMA_VERSION,
        operation_id=operation_id,
        state="preparing",
        intent="restore_staging",
        snapshot_root=str(snapshot.snapshot_root),
        active_root=str(active),
        staging_root=str(staging),
        rollback_root=str(rollback),
        displaced_root=str(displaced),
        snapshot_fingerprint=snapshot.source_fingerprint,
        original_fingerprint=original_fingerprint,
        original_fingerprint_kind="restore-source-logical-v1" if logical else "physical-v1",
        file_count=snapshot.file_count,
    )
    with _OperationLease(operation, operations):
        operation_path = operations / f"{operation_id}.json"
        if operation_path.exists() or operation_path.is_symlink():
            existing = load_vault_recovery_operation(
                operations_root=operations, operation_id=operation_id
            )
            if logical and (
                existing.original_fingerprint_kind != operation.original_fingerprint_kind
                or existing.original_fingerprint != expected_source_fingerprint
            ):
                raise VaultOperationalRecoveryConflict("Vault recovery confirmation replay drifted")
            _require_matching_replay(existing, operation)
            return existing
        if logical:
            if fingerprint_vault_restore_source(active) != expected_source_fingerprint:
                raise VaultOperationalRecoveryConflict("active Vault changed after restore confirmation")
        for candidate in (staging, rollback, displaced):
            if candidate.exists() or candidate.is_symlink():
                raise VaultOperationalRecoveryConflict("recovery sibling path already exists")
        _write_operation(operations, operation)
        try:
            restore_vault_backup(
                snapshot_root=snapshot.snapshot_root,
                target_root=staging,
                copy_file=copy_file,
            )
            compare_vault_to_backup(snapshot_root=snapshot.snapshot_root, vault_root=staging)
            if logical:
                _require_fingerprint(active, original_fingerprint, "active Vault", kind=operation.original_fingerprint_kind)
        except Exception as error:
            failed = _replace_operation(
                operation, state="failed", intent="none", last_error=type(error).__name__
            )
            _write_operation(operations, failed)
            raise VaultOperationalRecoveryError("staged Vault restore did not complete") from error
        prepared = _replace_operation(operation, state="prepared", intent="none")
        _write_operation(operations, prepared)
        return prepared


def adopt_prepared_vault(
    *,
    operations_root: Path,
    operation_id: str,
    application_offline: bool,
    fault_hook: Callable[[str], None] | None = None,
) -> VaultRecoveryOperation:
    if not application_offline:
        raise VaultOperationalRecoveryConflict("Vault adoption requires application offline confirmation")
    operations = _safe_absolute(operations_root)
    operation = load_vault_recovery_operation(operations_root=operations, operation_id=operation_id)
    with _OperationLease(operation, operations):
        operation = load_vault_recovery_operation(
            operations_root=operations, operation_id=operation_id
        )
        if operation.state not in {"prepared", "old_detached", "adopted"} and operation.intent not in {
            "detach_old",
            "adopt_staged",
        }:
            raise VaultOperationalRecoveryConflict(
                f"Vault recovery cannot adopt from {operation.state}"
            )
        operation = _reconcile_adoption(operation, operations)
        if operation.state == "adopted":
            return operation
        if operation.state != "prepared":
            raise VaultOperationalRecoveryConflict(f"Vault recovery cannot adopt from {operation.state}")
        compare_vault_to_backup(snapshot_root=Path(operation.snapshot_root), vault_root=Path(operation.staging_root))
        _require_fingerprint(Path(operation.active_root), operation.original_fingerprint, "active Vault", kind=operation.original_fingerprint_kind)
        operation = _replace_operation(operation, intent="detach_old")
        _write_operation(operations, operation)
        os.replace(operation.active_root, operation.rollback_root)
        if fault_hook:
            fault_hook("old_detached")
        operation = _replace_operation(operation, state="old_detached", intent="adopt_staged")
        _write_operation(operations, operation)
        os.replace(operation.staging_root, operation.active_root)
        if fault_hook:
            fault_hook("new_adopted")
        operation = _replace_operation(operation, state="adopted", intent="none")
        _write_operation(operations, operation)
        compare_vault_to_backup(snapshot_root=Path(operation.snapshot_root), vault_root=Path(operation.active_root))
        return operation


def rollback_adopted_vault(
    *,
    operations_root: Path,
    operation_id: str,
    application_offline: bool,
    fault_hook: Callable[[str], None] | None = None,
) -> VaultRecoveryOperation:
    if not application_offline:
        raise VaultOperationalRecoveryConflict("Vault rollback requires application offline confirmation")
    operations = _safe_absolute(operations_root)
    operation = load_vault_recovery_operation(operations_root=operations, operation_id=operation_id)
    with _OperationLease(operation, operations):
        operation = load_vault_recovery_operation(
            operations_root=operations, operation_id=operation_id
        )
        if operation.state not in {"adopted", "rollback_prepared", "rolled_back"} and operation.intent not in {
            "displace_adopted",
            "restore_old",
        }:
            raise VaultOperationalRecoveryConflict(
                f"Vault recovery cannot rollback from {operation.state}"
            )
        operation = _reconcile_rollback(operation, operations)
        if operation.state == "rolled_back":
            return operation
        if operation.state != "adopted":
            raise VaultOperationalRecoveryConflict(f"Vault recovery cannot rollback from {operation.state}")
        compare_vault_to_backup(
            snapshot_root=Path(operation.snapshot_root), vault_root=Path(operation.active_root)
        )
        _require_fingerprint(
            Path(operation.rollback_root), operation.original_fingerprint, "rollback Vault", kind=operation.original_fingerprint_kind
        )
        operation = _replace_operation(operation, state="rollback_prepared", intent="displace_adopted")
        _write_operation(operations, operation)
        os.replace(operation.active_root, operation.displaced_root)
        if fault_hook:
            fault_hook("adopted_displaced")
        operation = _replace_operation(operation, intent="restore_old")
        _write_operation(operations, operation)
        os.replace(operation.rollback_root, operation.active_root)
        if fault_hook:
            fault_hook("old_restored")
        operation = _replace_operation(operation, state="rolled_back", intent="none")
        _write_operation(operations, operation)
        return operation


def recover_vault_operation(
    *, operations_root: Path, operation_id: str, application_offline: bool
) -> VaultRecoveryOperation:
    if not application_offline:
        raise VaultOperationalRecoveryConflict("Vault recovery requires application offline confirmation")
    operations = _safe_absolute(operations_root)
    operation = load_vault_recovery_operation(operations_root=operations, operation_id=operation_id)
    if operation.state == "preparing":
        with _OperationLease(operation, operations):
            operation = load_vault_recovery_operation(
                operations_root=operations, operation_id=operation_id
            )
            return _recover_preparing(operation, operations)
    if operation.state in {"prepared", "old_detached"} or operation.intent in {"detach_old", "adopt_staged"}:
        return adopt_prepared_vault(
            operations_root=operations, operation_id=operation_id, application_offline=True
        )
    if operation.state == "rollback_prepared" or operation.intent in {"displace_adopted", "restore_old"}:
        return rollback_adopted_vault(
            operations_root=operations, operation_id=operation_id, application_offline=True
        )
    return operation


def load_vault_recovery_operation(*, operations_root: Path, operation_id: str) -> VaultRecoveryOperation:
    _require_operation_id(operation_id)
    path = _safe_absolute(operations_root) / f"{operation_id}.json"
    if path.is_symlink():
        raise VaultOperationalRecoveryError("Vault recovery operation cannot be a symlink")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        operation = VaultRecoveryOperation(**payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise VaultOperationalRecoveryError("Vault recovery operation is unreadable") from error
    if operation.schema_version != _SCHEMA_VERSION or operation.operation_id != operation_id or operation.state not in {
        "preparing", "prepared", "old_detached", "adopted", "rollback_prepared", "rolled_back", "failed"
    }:
        raise VaultOperationalRecoveryError("Vault recovery operation is invalid")
    if operation.original_fingerprint_kind not in {"physical-v1", "restore-source-logical-v1"}:
        raise VaultOperationalRecoveryError("Vault recovery fingerprint kind is invalid")
    _validate_operation_paths(operation, _safe_absolute(operations_root))
    return operation


def _recover_preparing(
    operation: VaultRecoveryOperation, operations: Path
) -> VaultRecoveryOperation:
    if operation.state != "preparing":
        return operation
    staging = Path(operation.staging_root)
    try:
        compare_vault_to_backup(
            snapshot_root=Path(operation.snapshot_root), vault_root=staging
        )
    except Exception as error:
        failed = _replace_operation(
            operation, state="failed", intent="none", last_error=type(error).__name__
        )
        _write_operation(operations, failed)
        return failed
    prepared = _replace_operation(operation, state="prepared", intent="none", last_error=None)
    _write_operation(operations, prepared)
    return prepared


def _reconcile_adoption(operation: VaultRecoveryOperation, operations: Path) -> VaultRecoveryOperation:
    active = Path(operation.active_root).exists()
    staging = Path(operation.staging_root).exists()
    rollback = Path(operation.rollback_root).exists()
    if active and not staging and rollback:
        compare_vault_to_backup(
            snapshot_root=Path(operation.snapshot_root), vault_root=Path(operation.active_root)
        )
        _require_fingerprint(
            Path(operation.rollback_root), operation.original_fingerprint, "rollback Vault", kind=operation.original_fingerprint_kind
        )
        adopted = _replace_operation(operation, state="adopted", intent="none")
        _write_operation(operations, adopted)
        return adopted
    if not active and staging and rollback:
        compare_vault_to_backup(
            snapshot_root=Path(operation.snapshot_root), vault_root=Path(operation.staging_root)
        )
        _require_fingerprint(
            Path(operation.rollback_root), operation.original_fingerprint, "rollback Vault", kind=operation.original_fingerprint_kind
        )
        detached = _replace_operation(operation, state="old_detached", intent="adopt_staged")
        _write_operation(operations, detached)
        os.replace(detached.staging_root, detached.active_root)
        compare_vault_to_backup(
            snapshot_root=Path(detached.snapshot_root), vault_root=Path(detached.active_root)
        )
        adopted = _replace_operation(detached, state="adopted", intent="none")
        _write_operation(operations, adopted)
        return adopted
    if active and staging and not rollback:
        _require_fingerprint(Path(operation.active_root), operation.original_fingerprint, "active Vault", kind=operation.original_fingerprint_kind)
        compare_vault_to_backup(
            snapshot_root=Path(operation.snapshot_root), vault_root=Path(operation.staging_root)
        )
        prepared = _replace_operation(operation, state="prepared", intent="none")
        _write_operation(operations, prepared)
        return prepared
    if operation.state == "adopted" and active and rollback:
        compare_vault_to_backup(
            snapshot_root=Path(operation.snapshot_root), vault_root=Path(operation.active_root)
        )
        _require_fingerprint(
            Path(operation.rollback_root), operation.original_fingerprint, "rollback Vault", kind=operation.original_fingerprint_kind
        )
        return operation
    raise VaultOperationalRecoveryConflict("Vault adoption filesystem state is contradictory")


def _reconcile_rollback(operation: VaultRecoveryOperation, operations: Path) -> VaultRecoveryOperation:
    active = Path(operation.active_root).exists()
    rollback = Path(operation.rollback_root).exists()
    displaced = Path(operation.displaced_root).exists()
    if active and not rollback and displaced:
        _require_fingerprint(Path(operation.active_root), operation.original_fingerprint, "active Vault", kind=operation.original_fingerprint_kind)
        compare_vault_to_backup(
            snapshot_root=Path(operation.snapshot_root), vault_root=Path(operation.displaced_root)
        )
        rolled_back = _replace_operation(operation, state="rolled_back", intent="none")
        _write_operation(operations, rolled_back)
        return rolled_back
    if not active and rollback and displaced:
        _require_fingerprint(
            Path(operation.rollback_root), operation.original_fingerprint, "rollback Vault", kind=operation.original_fingerprint_kind
        )
        compare_vault_to_backup(
            snapshot_root=Path(operation.snapshot_root), vault_root=Path(operation.displaced_root)
        )
        os.replace(operation.rollback_root, operation.active_root)
        rolled_back = _replace_operation(operation, state="rolled_back", intent="none")
        _write_operation(operations, rolled_back)
        return rolled_back
    if operation.state == "adopted" and active and rollback and not displaced:
        compare_vault_to_backup(
            snapshot_root=Path(operation.snapshot_root), vault_root=Path(operation.active_root)
        )
        _require_fingerprint(
            Path(operation.rollback_root), operation.original_fingerprint, "rollback Vault", kind=operation.original_fingerprint_kind
        )
        return operation
    raise VaultOperationalRecoveryConflict("Vault rollback filesystem state is contradictory")


class _OperationLease:
    def __init__(self, operation: VaultRecoveryOperation, operations: Path) -> None:
        self._operation = operation
        self._path = Path(operation.active_root).parent / ".chriptmas-vault-recovery.lock"
        self._operations = operations

    def __enter__(self) -> None:
        payload = json.dumps({"operation_id": self._operation.operation_id, "pid": os.getpid()})
        for _attempt in range(2):
            try:
                descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not _remove_stale_lease(self._path, self._operation.operation_id):
                    raise VaultOperationalRecoveryConflict("another Vault recovery operation holds the lease")
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            return None
        raise VaultOperationalRecoveryConflict("Vault recovery lease could not be acquired")

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._path.unlink(missing_ok=True)


def _remove_stale_lease(path: Path, operation_id: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        owner = int(payload.get("pid", 0))
        owner_operation = payload.get("operation_id")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if owner_operation != operation_id or _pid_alive(owner):
        return False
    path.unlink(missing_ok=True)
    return True


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_operation(operations: Path, operation: VaultRecoveryOperation) -> None:
    operations.mkdir(parents=True, exist_ok=True)
    path = operations / f"{operation.operation_id}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".vault-operation-", suffix=".tmp", dir=operations)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(asdict(operation), output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _replace_operation(operation: VaultRecoveryOperation, **changes: object) -> VaultRecoveryOperation:
    payload = asdict(operation)
    payload.update(changes)
    return VaultRecoveryOperation(**payload)


def _existing_directory(value: Path, label: str) -> Path:
    path = _safe_absolute(value)
    if not path.is_dir() or path.is_symlink():
        raise VaultOperationalRecoveryError(f"{label} must be a non-symlink directory")
    return path


def _safe_absolute(value: Path) -> Path:
    return value.expanduser().absolute().resolve(strict=False)


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _paths_overlap(first: Path, second: Path) -> bool:
    return _contains(first, second) or _contains(second, first)


def _require_fingerprint(path: Path, expected: str, label: str, *, kind: str = "physical-v1") -> None:
    try:
        if kind == "physical-v1":
            actual = fingerprint_vault_root(path)
        elif kind == "restore-source-logical-v1":
            actual = fingerprint_vault_restore_source(path)
        else:
            raise VaultOperationalRecoveryError("Vault recovery fingerprint kind is invalid")
    except Exception as error:
        raise VaultOperationalRecoveryConflict(f"{label} cannot be verified") from error
    if actual != expected:
        raise VaultOperationalRecoveryConflict(f"{label} fingerprint drifted")


def _require_matching_replay(
    existing: VaultRecoveryOperation, requested: VaultRecoveryOperation
) -> None:
    fields = (
        "snapshot_root",
        "active_root",
        "staging_root",
        "rollback_root",
        "displaced_root",
        "snapshot_fingerprint",
        "file_count",
    )
    if any(getattr(existing, field) != getattr(requested, field) for field in fields):
        raise VaultOperationalRecoveryConflict("Vault recovery operation replay drifted")
    if existing.state in {"preparing", "prepared", "failed"}:
        _require_fingerprint(
            Path(existing.active_root), existing.original_fingerprint, "active Vault", kind=existing.original_fingerprint_kind
        )
    elif existing.state == "adopted":
        compare_vault_to_backup(
            snapshot_root=Path(existing.snapshot_root), vault_root=Path(existing.active_root)
        )
        _require_fingerprint(
            Path(existing.rollback_root), existing.original_fingerprint, "rollback Vault", kind=existing.original_fingerprint_kind
        )
    elif existing.state == "rolled_back":
        _require_fingerprint(
            Path(existing.active_root), existing.original_fingerprint, "active Vault", kind=existing.original_fingerprint_kind
        )
        compare_vault_to_backup(
            snapshot_root=Path(existing.snapshot_root), vault_root=Path(existing.displaced_root)
        )


def _validate_operation_paths(operation: VaultRecoveryOperation, operations: Path) -> None:
    active = _safe_absolute(Path(operation.active_root))
    snapshot = _safe_absolute(Path(operation.snapshot_root))
    expected = {
        "staging_root": active.parent / f".{active.name}.restore-{operation.operation_id}",
        "rollback_root": active.parent / f".{active.name}.rollback-{operation.operation_id}",
        "displaced_root": active.parent / f".{active.name}.displaced-{operation.operation_id}",
    }
    if any(_safe_absolute(Path(getattr(operation, field))) != path for field, path in expected.items()):
        raise VaultOperationalRecoveryError("Vault recovery operation paths are invalid")
    if _paths_overlap(snapshot, active) or _paths_overlap(active, operations):
        raise VaultOperationalRecoveryError("Vault recovery operation roots overlap")


def _require_operation_id(value: str) -> None:
    if not isinstance(value, str) or not _SAFE_OPERATION_ID.fullmatch(value):
        raise VaultOperationalRecoveryError("Vault recovery operation id is invalid")
