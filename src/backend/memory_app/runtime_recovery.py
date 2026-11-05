from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from backend.shared.deployment import runtime_backup_roots
from core.product_core.local_memory_vault import MemorySnapshotError, RollbackToMemorySnapshot
from core.storage_provider.vault_backup_restore import (
    VaultBackupRestoreError,
    _require_non_symlink_path,
    compare_vault_to_backup,
    fingerprint_vault_entry,
    fingerprint_vault_restore_source,
    fingerprint_vault_root,
    is_runtime_backup,
    read_backup_catalog,
    verify_vault_backup,
)
from core.storage_provider.vault_operational_recovery import (
    VaultOperationalRecoveryConflict,
    VaultOperationalRecoveryError,
    _OperationLease,
    _read_operation_payload,
    _write_operation,
    load_vault_recovery_operation,
)

from .backup import fingerprint_runtime_restore_source, restore_runtime, runtime_backup_exclusions


_OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_STATES = {"preparing", "prepared", "queued", "adopting", "adopted", "failed"}
_TERMINAL = {"adopted", "rolled_back", "failed"}
_CONTROL_NAMES = {"..rebuild-data-recovery", ".chriptmas-vault-recovery.lock"}


class RuntimeRestoreArchiveInvalid(VaultOperationalRecoveryConflict):
    """备份归档内容已明确无法证明原恢复资格。"""


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryOperation:
    schema_version: str
    kind: str
    operation_id: str
    state: str
    intent: str
    snapshot_root: str
    active_root: str
    staging_root: str
    rollback_root: str
    snapshot_fingerprint: str
    original_fingerprint: str
    file_count: int
    created_at: str
    target_entries: tuple[dict[str, str], ...] = ()
    original_entries: tuple[dict[str, str], ...] = ()
    last_error: str | None = None
    activation_state: str = "pending"
    server_identity: dict[str, object] | None = None


def recovery_operation_kind(*, operations_root: Path, operation_id: str) -> str:
    payload = _read_payload(operations_root, operation_id)
    kind = payload.get("kind", "vault")
    if not isinstance(kind, str) or kind not in {"vault", "runtime"}:
        raise VaultOperationalRecoveryError("recovery operation kind is invalid")
    return kind


def load_runtime_recovery_operation(*, operations_root: Path, operation_id: str) -> RuntimeRecoveryOperation:
    payload = _read_payload(operations_root, operation_id)
    try:
        payload["target_entries"] = _entries(payload.get("target_entries", []))
        payload["original_entries"] = _entries(payload.get("original_entries", []))
        operation = RuntimeRecoveryOperation(**payload)
    except (TypeError, ValueError) as error:
        raise VaultOperationalRecoveryError("runtime recovery operation is invalid") from error
    if (operation.schema_version != "1.0.0" or operation.kind != "runtime"
            or operation.operation_id != operation_id or not isinstance(operation.state, str) or operation.state not in _STATES
            or not isinstance(operation.intent, str)
            or not isinstance(operation.activation_state, str) or operation.activation_state not in {"pending", "ready", "failed"}
            or operation.activation_state == "ready" and operation.state != "adopted"
            or not isinstance(operation.file_count, int) or operation.file_count < 0
            or not isinstance(operation.snapshot_fingerprint, str) or _FINGERPRINT.fullmatch(operation.snapshot_fingerprint) is None
            or not isinstance(operation.original_fingerprint, str) or _FINGERPRINT.fullmatch(operation.original_fingerprint) is None):
        raise VaultOperationalRecoveryError("runtime recovery operation is invalid")
    active = _safe_path(operation.active_root)
    _working, snapshots, operations = runtime_backup_roots(active)
    management = operations.parent
    expected = {
        "staging_root": management / "restored" / operation_id,
        "rollback_root": management / "rollback" / operation_id,
    }
    if (_safe_path(operations_root) != _safe_path(operations)
            or any(_safe_path(getattr(operation, key)) != value for key, value in expected.items())
            or _safe_path(operation.snapshot_root).parent != _safe_path(snapshots)):
        raise VaultOperationalRecoveryError("runtime recovery operation paths are invalid")
    if operation.intent != "none":
        verb, separator, name = operation.intent.partition(":")
        entries = operation.original_entries if verb == "detach" else operation.target_entries
        if not separator or verb not in {"detach", "adopt"} or name not in {entry["name"] for entry in entries}:
            raise VaultOperationalRecoveryError("runtime recovery intent is invalid")
    if operation.server_identity is not None:
        authority = operation.server_identity
        if (not isinstance(authority, dict) or set(authority) != {
                'actor_user_id', 'actor_device_id', 'actor_device_revision', 'target_user_id', 'target_user_revision'}
                or any(not isinstance(authority[key], str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}', authority[key]) is None
                       for key in ('actor_user_id', 'actor_device_id', 'target_user_id'))
                or any(type(authority[key]) is not int or authority[key] < 1
                       for key in ('actor_device_revision', 'target_user_revision'))):
            raise VaultOperationalRecoveryError("runtime recovery authority is invalid")
    return operation


def ensure_no_pending_recovery(*, operations_root: Path, operation_id: str) -> None:
    operations = _safe_path(operations_root)
    if not operations.exists():
        return
    if not operations.is_dir():
        raise VaultOperationalRecoveryError("recovery operations root is invalid")
    for path in sorted(operations.glob("*.json")):
        if path.stem == operation_id:
            continue
        if recovery_operation_kind(operations_root=operations, operation_id=path.stem) == "runtime":
            other = load_runtime_recovery_operation(operations_root=operations, operation_id=path.stem)
        else:
            other = load_vault_recovery_operation(operations_root=operations, operation_id=path.stem)
        if other.state not in _TERMINAL:
            raise VaultOperationalRecoveryConflict("another recovery operation is pending")


def build_restore_plan(root, snapshot_id, *, store, namespace_id, require_runtime_archive=False):
    active, snapshots, operations = runtime_backup_roots(root)
    snapshot_root = snapshots / snapshot_id
    try:
        record = read_backup_catalog(snapshot_root, strict_io=require_runtime_archive)
        if record is None:
            if require_runtime_archive:
                raise MemorySnapshotError("full recovery point payload or catalog is missing")
            record = store.read("memory_snapshots", snapshot_id)
            if isinstance(record, Mapping) and record.get("restorable") is True:
                raise MemorySnapshotError("full recovery point payload or catalog is missing")
        if isinstance(record, Mapping) and record.get("namespace_id") != namespace_id:
            raise MemorySnapshotError("recovery point does not belong to current namespace")
        runtime = (isinstance(record, Mapping) and record.get("restorable") is True
                   and is_runtime_backup(snapshot_root, strict_io=require_runtime_archive))
        if require_runtime_archive and not runtime:
            raise MemorySnapshotError("runtime recovery requires a full runtime snapshot")
        if runtime:
            snapshot = verify_vault_backup(snapshot_root, strict_io=require_runtime_archive)
            if snapshot.snapshot_id != snapshot_id or snapshot.source_fingerprint != record.get("vault_fingerprint"):
                raise MemorySnapshotError("runtime snapshot identity does not match recovery point")
    except (MemorySnapshotError, VaultBackupRestoreError) as error:
        if not require_runtime_archive:
            raise
        raise RuntimeRestoreArchiveInvalid("runtime_restore_archive_invalid") from error
    fingerprint = fingerprint_runtime_restore_source(root) if runtime else fingerprint_vault_restore_source(active)
    plan = RollbackToMemorySnapshot(store, namespace_id=namespace_id).execute(
        target_snapshot_id=snapshot_id, current_vault_fingerprint=fingerprint, snapshot_record=record,
    )
    if not plan.target_vault_fingerprint:
        raise MemorySnapshotError("legacy manifest-only snapshot cannot restore Vault contents")
    return plan, runtime, active, snapshots, operations


def server_restore_operation_id(rollback_id, authority):
    identity = json.dumps(authority, sort_keys=True, separators=(',', ':'))
    return 'restore-' + uuid5(NAMESPACE_URL, 'chriptmas-runtime-restore:' + rollback_id + ':' + identity).hex


def prepare_runtime_recovery(
    *, snapshot_root: Path, active_root: Path, operations_root: Path, operation_id: str,
    expected_source_fingerprint: str, server_identity=None,
) -> RuntimeRecoveryOperation:
    _require_operation_id(operation_id)
    active = _safe_path(active_root)
    working, snapshots, operations = runtime_backup_roots(active)
    if (not active.is_dir() or not working.is_dir() or _safe_path(operations_root) != operations
            or _safe_path(snapshot_root).parent != snapshots):
        raise VaultOperationalRecoveryError("runtime recovery roots are invalid")
    snapshot = verify_vault_backup(snapshot_root)
    if not is_runtime_backup(snapshot.snapshot_root):
        raise VaultOperationalRecoveryError("runtime recovery requires a full runtime snapshot")
    if _FINGERPRINT.fullmatch(expected_source_fingerprint) is None:
        raise VaultOperationalRecoveryError("runtime recovery source identity is invalid")
    management = operations.parent
    operation = RuntimeRecoveryOperation(
        schema_version="1.0.0", kind="runtime", operation_id=operation_id,
        state="preparing", intent="none", snapshot_root=str(snapshot.snapshot_root),
        active_root=str(active), staging_root=str(management / "restored" / operation_id),
        rollback_root=str(management / "rollback" / operation_id),
        snapshot_fingerprint=snapshot.source_fingerprint,
        original_fingerprint=expected_source_fingerprint, file_count=snapshot.file_count,
        created_at=datetime.now(timezone.utc).isoformat(),
        server_identity=server_identity,
    )
    with _OperationLease(operation, operations, lock_root=active):
        ensure_no_pending_recovery(operations_root=operations, operation_id=operation_id)
        path = operations / f"{operation_id}.json"
        if path.exists():
            existing = load_runtime_recovery_operation(operations_root=operations, operation_id=operation_id)
            if any(getattr(existing, key) != getattr(operation, key) for key in (
                "snapshot_root", "active_root", "snapshot_fingerprint", "original_fingerprint", "file_count",
            )):
                raise VaultOperationalRecoveryConflict("runtime recovery confirmation replay drifted")
            if existing.state not in {"prepared", "queued"}:
                raise VaultOperationalRecoveryConflict("runtime recovery is not prepared")
            if existing.server_identity is not None and existing.server_identity != server_identity:
                raise VaultOperationalRecoveryConflict("runtime recovery authority changed")
            _require_source(existing)
            _verify_staging(existing)
            return existing
        _require_source(operation)
        for target in (Path(operation.staging_root), Path(operation.rollback_root)):
            _safe_path(target)
            if target.exists():
                raise VaultOperationalRecoveryConflict("runtime recovery target already exists")
        _write_operation(operations, operation)
        try:
            staging = Path(operation.staging_root)
            staging.parent.mkdir(parents=True, exist_ok=True)
            restore_runtime(snapshot.snapshot_root, staging)
            _verify_staging(operation)
            _require_source(operation)
            prepared = replace(operation, state="prepared", target_entries=_capture_entries(staging))
            _write_operation(operations, prepared)
            return prepared
        except (OSError, ValueError) as error:
            _write_operation(operations, replace(operation, state="failed", last_error=type(error).__name__))
            raise VaultOperationalRecoveryError("runtime restore preparation did not complete") from error


def adopt_prepared_runtime(
    *, operations_root: Path, operation_id: str, working_root: Path, application_offline: bool,
    authorize_adoption=None,
) -> RuntimeRecoveryOperation:
    if not application_offline:
        raise VaultOperationalRecoveryConflict("runtime adoption requires application offline confirmation")
    operations = _safe_path(operations_root)
    operation = load_runtime_recovery_operation(operations_root=operations, operation_id=operation_id)
    working = _safe_path(working_root)
    if working.name != ".rebuild-data" or Path(operation.active_root) != working.parent:
        raise VaultOperationalRecoveryError("runtime recovery does not belong to working root")
    active = Path(operation.active_root)
    with _OperationLease(operation, operations, lock_root=active):
        operation = load_runtime_recovery_operation(operations_root=operations, operation_id=operation_id)
        if operation.state == "adopted":
            return operation
        if operation.state not in {"preparing", "prepared", "queued", "adopting"}:
            raise VaultOperationalRecoveryConflict("runtime recovery cannot be adopted")
        if operation.state != "adopting" and operation.server_identity is not None and authorize_adoption is None:
            raise VaultOperationalRecoveryConflict("server runtime adoption requires current authority")
        try:
            if operation.state == "preparing":
                _verify_staging(operation)
                operation = replace(operation, state="prepared", target_entries=_capture_entries(Path(operation.staging_root)))
                _write_operation(operations, operation)
            if operation.state in {"prepared", "queued"}:
                _require_source(operation)
                _verify_staging(operation)
                _require_entries(Path(operation.staging_root), operation.target_entries)
                fingerprint_vault_root(active, exclude_roots=_controls(active))
                original = _capture_entries(active, exclude_controls=True)
                rollback = Path(operation.rollback_root)
                rollback.parent.mkdir(parents=True, exist_ok=True)
                authority = authorize_adoption(operation) if authorize_adoption is not None else nullcontext()
                with authority:
                    rollback.mkdir(exist_ok=True)
                    if any(rollback.iterdir()):
                        raise VaultOperationalRecoveryConflict("runtime rollback root is not empty")
                    operation = replace(operation, state="adopting", original_entries=original)
                    _write_operation(operations, operation)
            operation = _adopt_entries(operation, operations)
            compare_vault_to_backup(
                snapshot_root=Path(operation.snapshot_root), vault_root=active,
                exclude_roots=runtime_backup_exclusions(active), exclude_logs=True,
            )
            _require_entries(Path(operation.rollback_root), operation.original_entries)
            adopted = replace(operation, state="adopted", intent="none", last_error=None)
            _write_operation(operations, adopted)
            return adopted
        except (OSError, ValueError) as error:
            operation = load_runtime_recovery_operation(operations_root=operations, operation_id=operation_id)
            failed = replace(
                operation, state="failed" if operation.state in {"preparing", "prepared", "queued"} else operation.state,
                last_error=(str(error) if str(error) == "runtime_restore_authority_changed" else type(error).__name__),
            )
            _write_operation(operations, failed)
            raise VaultOperationalRecoveryError("runtime adoption did not complete") from error


def runtime_recovery_summary(*, operations_root: Path) -> list[dict[str, object]]:
    operations = _safe_path(operations_root)
    if not operations.exists():
        return []
    results = []
    for path in sorted(operations.glob("*.json")):
        if recovery_operation_kind(operations_root=operations, operation_id=path.stem) != "runtime":
            continue
        operation = load_runtime_recovery_operation(operations_root=operations, operation_id=path.stem)
        results.append({
            "operation_id": operation.operation_id, "target_snapshot_id": Path(operation.snapshot_root).name,
            "state": operation.state, "restore_executed": operation.state == "adopted",
            "runtime_ready": operation.activation_state == "ready",
            "created_at": operation.created_at, "error_code": "runtime_restore_failed" if operation.last_error or operation.activation_state == "failed" else None,
        })
    return sorted(results, key=lambda value: value["created_at"])


def queue_runtime_recovery(
    *, operations_root: Path, operation_id: str, server_identity=None, verified_legacy=None,
) -> RuntimeRecoveryOperation:
    operations = _safe_path(operations_root)
    operation = load_runtime_recovery_operation(operations_root=operations, operation_id=operation_id)
    with _OperationLease(operation, operations, lock_root=Path(operation.active_root)):
        operation = load_runtime_recovery_operation(operations_root=operations, operation_id=operation_id)
        if operation.state not in {"prepared", "queued"}:
            raise VaultOperationalRecoveryConflict("runtime recovery is not prepared")
        if operation.server_identity != server_identity and not (
            operation.server_identity is None and operation.state == "prepared" and operation == verified_legacy
        ):
            raise VaultOperationalRecoveryConflict("runtime recovery authority changed")
        try:
            _require_source(operation)
        except VaultOperationalRecoveryConflict:
            _write_operation(operations, replace(operation, state="failed", activation_state="pending",
                                                  last_error="runtime_source_changed"))
            raise
        queued = replace(operation, state="queued", server_identity=server_identity)
        _write_operation(operations, queued)
        return queued


def invalidate_runtime_preparation(
    *, operations_root: Path, operation: RuntimeRecoveryOperation, reason="runtime_restore_authority_changed",
) -> None:
    with _OperationLease(operation, operations_root, lock_root=Path(operation.active_root)):
        current = load_runtime_recovery_operation(operations_root=operations_root, operation_id=operation.operation_id)
        if current.state == "prepared" and current == operation:
            _write_operation(operations_root, replace(current, state="failed", last_error=reason))


def record_runtime_activation(*, operations_root: Path, operation_id: str, ready: bool) -> RuntimeRecoveryOperation:
    operations = _safe_path(operations_root)
    operation = load_runtime_recovery_operation(operations_root=operations, operation_id=operation_id)
    with _OperationLease(operation, operations, lock_root=Path(operation.active_root)):
        operation = load_runtime_recovery_operation(operations_root=operations, operation_id=operation_id)
        if ready and operation.state != "adopted":
            raise VaultOperationalRecoveryConflict("runtime adoption is not complete")
        result = replace(operation, activation_state="ready" if ready else "failed",
                         last_error=None if ready else operation.last_error or "runtime_activation_failed")
        _write_operation(operations, result)
        return result


def fail_runtime_execution(*, operations_root: Path, operation_id: str, reason="runtime_restore_authority_changed") -> None:
    operation = load_runtime_recovery_operation(operations_root=operations_root, operation_id=operation_id)
    with _OperationLease(operation, operations_root, lock_root=Path(operation.active_root)):
        operation = load_runtime_recovery_operation(operations_root=operations_root, operation_id=operation_id)
        state = "failed" if operation.state in {"prepared", "queued"} else operation.state
        _write_operation(operations_root, replace(operation, state=state, activation_state="failed",
                                                  last_error=reason))


def reconcile_runtime_preparation(*, operations_root: Path, operation_id: str) -> RuntimeRecoveryOperation:
    operation = load_runtime_recovery_operation(operations_root=operations_root, operation_id=operation_id)
    with _OperationLease(operation, operations_root, lock_root=Path(operation.active_root)):
        operation = load_runtime_recovery_operation(operations_root=operations_root, operation_id=operation_id)
        if operation.state != "preparing":
            return operation
        try:
            _verify_staging(operation)
            _require_source(operation)
            result = replace(operation, state="prepared", target_entries=_capture_entries(Path(operation.staging_root)))
        except (OSError, ValueError):
            result = replace(operation, state="failed", activation_state="pending", last_error="runtime_preparation_interrupted")
        _write_operation(operations_root, result)
        return result


def _adopt_entries(operation: RuntimeRecoveryOperation, operations: Path) -> RuntimeRecoveryOperation:
    active, staging, rollback = (Path(getattr(operation, key)) for key in ("active_root", "staging_root", "rollback_root"))
    targets = {entry["name"]: entry for entry in operation.target_entries}
    for entry in operation.original_entries:
        current, retained = active / entry["name"], rollback / entry["name"]
        if retained.exists():
            _require_entry(retained, entry)
            if current.exists():
                if entry["name"] not in targets or (staging / entry["name"]).exists():
                    raise VaultOperationalRecoveryConflict("runtime original entry is ambiguous")
                _require_entry(current, targets[entry["name"]])
            continue
        _require_entry(current, entry)
        operation = replace(operation, intent=f"detach:{entry['name']}")
        _write_operation(operations, operation)
        os.replace(current, retained)
        _require_entry(retained, entry)
        operation = replace(operation, intent="none")
        _write_operation(operations, operation)
    for entry in operation.target_entries:
        source, target = staging / entry["name"], active / entry["name"]
        if target.exists() and not source.exists():
            _require_entry(target, entry)
            continue
        if target.exists() or not source.exists():
            raise VaultOperationalRecoveryConflict("runtime target entry is ambiguous")
        _require_entry(source, entry)
        operation = replace(operation, intent=f"adopt:{entry['name']}")
        _write_operation(operations, operation)
        os.replace(source, target)
        _require_entry(target, entry)
        operation = replace(operation, intent="none")
        _write_operation(operations, operation)
    return operation


def _require_source(operation: RuntimeRecoveryOperation) -> None:
    if fingerprint_runtime_restore_source(Path(operation.active_root)) != operation.original_fingerprint:
        raise VaultOperationalRecoveryConflict("runtime changed after restore confirmation")


def _verify_staging(operation: RuntimeRecoveryOperation) -> None:
    result = compare_vault_to_backup(snapshot_root=Path(operation.snapshot_root), vault_root=Path(operation.staging_root))
    if result.source_fingerprint != operation.snapshot_fingerprint or result.file_count != operation.file_count:
        raise VaultOperationalRecoveryConflict("runtime recovery snapshot identity drifted")


def _controls(active: Path) -> tuple[Path, ...]:
    return runtime_backup_roots(active)[2].parent, active / ".chriptmas-vault-recovery.lock"


def _capture_entries(root: Path, *, exclude_controls: bool = False) -> tuple[dict[str, str], ...]:
    return _entries([{"name": path.name, "kind": "directory" if path.is_dir() else "file",
                      "fingerprint": fingerprint_vault_entry(path)}
                     for path in sorted(root.iterdir(), key=lambda path: path.name)
                     if not exclude_controls or path.name not in _CONTROL_NAMES])


def _require_entries(root: Path, entries: tuple[dict[str, str], ...]) -> None:
    if {path.name for path in root.iterdir()} != {entry["name"] for entry in entries}:
        raise VaultOperationalRecoveryConflict("runtime recovery entry set drifted")
    for entry in entries:
        _require_entry(root / entry["name"], entry)


def _require_entry(path: Path, entry: dict[str, str]) -> None:
    _safe_path(path)
    if (not path.exists() or path.is_dir() != (entry["kind"] == "directory")
            or fingerprint_vault_entry(path) != entry["fingerprint"]):
        raise VaultOperationalRecoveryConflict("runtime recovery entry identity drifted")


def _entries(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("runtime entries are invalid")
    entries = []
    names = set()
    for entry in value:
        if (not isinstance(entry, dict) or set(entry) != {"name", "kind", "fingerprint"}
                or not isinstance(entry["name"], str) or not entry["name"]
                or entry["name"] in {".", "..", *_CONTROL_NAMES}
                or Path(entry["name"]).name != entry["name"] or "/" in entry["name"] or "\\" in entry["name"]
                or entry["kind"] not in {"file", "directory"}
                or not isinstance(entry["fingerprint"], str) or _FINGERPRINT.fullmatch(entry["fingerprint"]) is None
                or os.path.normcase(entry["name"]) in names):
            raise ValueError("runtime recovery entry is invalid")
        names.add(os.path.normcase(entry["name"]))
        entries.append(entry)
    return tuple(entries)


def _read_payload(operations_root: Path, operation_id: str) -> dict:
    _require_operation_id(operation_id)
    path = _safe_path(operations_root) / f"{operation_id}.json"
    if path.is_symlink():
        raise VaultOperationalRecoveryError("recovery operation entry is unsafe")
    try:
        payload = _read_operation_payload(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise VaultOperationalRecoveryError("recovery operation is unreadable") from error
    if not isinstance(payload, dict):
        raise VaultOperationalRecoveryError("recovery operation is invalid")
    return payload


def _safe_path(value: str | Path) -> Path:
    try:
        return _require_non_symlink_path(Path(value), label="runtime recovery path")
    except (TypeError, VaultBackupRestoreError) as error:
        raise VaultOperationalRecoveryError("runtime recovery path is unsafe") from error


def _require_operation_id(value: str) -> None:
    if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
        raise VaultOperationalRecoveryError("runtime recovery operation id is invalid")
